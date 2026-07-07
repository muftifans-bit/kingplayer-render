from __future__ import annotations

import os
import re
import threading
from typing import Dict, Iterable, Optional
from urllib.parse import quote, urljoin, urlparse

import requests
from flask import Flask, Response, make_response, request, stream_with_context

app = Flask(__name__)

# -----------------------------
# Paths
# -----------------------------
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")

M3U_PATH = os.path.join(DATA_DIR, "default_channels.m3u8")
PLAYER_V2_PATH = os.path.join(DATA_DIR, "player_v2.html")
PLAYER_PATH = os.path.join(DATA_DIR, "player.html")

# -----------------------------
# Proxy Settings
# -----------------------------
DEFAULT_UA = "VLC/3.0.20 LibVLC/3.0.20"
UPSTREAM_TIMEOUT = (8.0, 30.0)  # (connect, read)
STREAM_CHUNK_SIZE = 256 * 1024

# -----------------------------
# Thread-local Session (Connection Pool)
# -----------------------------
_tls = threading.local()


def get_session() -> requests.Session:
    sess = getattr(_tls, "session", None)
    if sess is None:
        sess = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=200,
            pool_maxsize=400,
            max_retries=0,
            pool_block=False,
        )
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
        _tls.session = sess
    return sess


# -----------------------------
# CORS
# -----------------------------
CORS_ALLOW_METHODS = "GET, HEAD, OPTIONS"
CORS_ALLOW_HEADERS = (
    "Origin, Referer, User-Agent, Content-Type, Accept, Accept-Encoding, "
    "Range, Cache-Control, If-None-Match, If-Modified-Since, Connection, Host, "
    "X-Requested-With"
)
CORS_EXPOSE_HEADERS = (
    "Content-Type, Content-Length, Accept-Ranges, Content-Range, ETag, "
    "Last-Modified, Cache-Control"
)


@app.after_request
def add_cors(resp: Response) -> Response:
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = CORS_ALLOW_METHODS
    resp.headers["Access-Control-Allow-Headers"] = CORS_ALLOW_HEADERS
    resp.headers["Access-Control-Expose-Headers"] = CORS_EXPOSE_HEADERS
    resp.headers["Access-Control-Max-Age"] = "86400"
    # Avoid buffering in some reverse proxies
    resp.headers["X-Accel-Buffering"] = "no"
    return resp


def cors_preflight() -> Response:
    return make_response("", 204)


# -----------------------------
# Helpers
# -----------------------------
def is_http_url(u: str) -> bool:
    return isinstance(u, str) and u.startswith(("http://", "https://"))


def is_proxy_url(u: str) -> bool:
    return isinstance(u, str) and u.startswith("/api/stream/proxy?url=")


def make_proxy_url(u: str) -> str:
    return "/api/stream/proxy?url=" + quote(u, safe="")


def header_get(name: str) -> Optional[str]:
    v = request.headers.get(name)
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def build_upstream_headers(upstream_url: str) -> Dict[str, str]:
    """
    Forward all required important headers:
      User-Agent, Referer, Origin, Accept, Accept-Encoding, Range,
      Cache-Control, If-Modified-Since, If-None-Match, Connection, Host
    """
    h: Dict[str, str] = {}

    h["User-Agent"] = header_get("User-Agent") or DEFAULT_UA

    for k in (
        "Referer",
        "Origin",
        "Accept",
        "Accept-Encoding",
        "Range",
        "Cache-Control",
        "If-Modified-Since",
        "If-None-Match",
        "Connection",
    ):
        v = header_get(k)
        if v:
            h[k] = v

    # Host header should match upstream host (do not forward our own host)
    try:
        parsed = urlparse(upstream_url)
        if parsed.netloc:
            h["Host"] = parsed.netloc
    except Exception:
        pass

    return h


def guess_is_m3u8(url: str, content_type: str) -> bool:
    u = (url or "").lower()
    ct = (content_type or "").lower()
    if ".m3u8" in u:
        return True
    if "mpegurl" in ct or "application/vnd.apple.mpegurl" in ct or "application/x-mpegurl" in ct:
        return True
    # some origins incorrectly return text/plain for m3u8
    if ct.startswith("text/") and ".m3u8" in u:
        return True
    return False


def copy_response_headers(up: requests.Response, is_playlist: bool) -> Dict[str, str]:
    """
    Copy important response headers. Avoid hop-by-hop headers.
    For playlist rewriting: do not forward Content-Encoding/Content-Length from upstream.
    """
    wanted = {
        "Content-Type",
        "Content-Length",
        "Accept-Ranges",
        "Content-Range",
        "ETag",
        "Last-Modified",
        "Cache-Control",
        "Content-Encoding",
    }
    out: Dict[str, str] = {}

    for k, v in (up.headers or {}).items():
        if not k:
            continue
        kk = k.title()
        if kk in wanted and v is not None:
            out[kk] = v

    if is_playlist:
        out.pop("Content-Encoding", None)
        out.pop("Content-Length", None)
        out["Content-Type"] = "application/vnd.apple.mpegurl"
        out["Cache-Control"] = "no-store"
    else:
        out.setdefault("Cache-Control", "no-store")

    return out


# -----------------------------
# M3U8 Rewriting (Full HLS / LL-HLS)
# -----------------------------
# Rewrite URI="...":
RE_URI_QUOTED = re.compile(r'URI="([^"]+)"')
# Rewrite URI=... (unquoted):
RE_URI_UNQUOTED = re.compile(r"\bURI=([^\", \t\r\n]+)")


def rewrite_m3u8(text: str, base_url: str) -> str:
    """
    Rewrite every URL reference to proxy:
      - Non-comment lines: segments/variants/subtitles/etc.
      - URI attributes: EXT-X-KEY, EXT-X-MAP, EXT-X-MEDIA, EXT-X-I-FRAME-STREAM-INF,
        EXT-X-PRELOAD-HINT, EXT-X-PART, RENDITION-REPORT, SESSION-KEY, IMAGE-STREAM-INF, etc.
    Preserve playlist structure and comments (do not remove any tag).
    """
    out_lines = []

    for raw in text.splitlines():
        line = raw.rstrip("\n")
        s = line.strip()

        if not s:
            out_lines.append(line)
            continue

        if s.startswith("#"):
            # Rewrite URI="...":
            def repl_quoted(m: re.Match) -> str:
                uri = m.group(1)
                if is_proxy_url(uri):
                    return f'URI="{uri}"'
                absu = urljoin(base_url, uri)
                return f'URI="{make_proxy_url(absu)}"'

            # Rewrite URI=...:
            def repl_unquoted(m: re.Match) -> str:
                uri = m.group(1)
                if is_proxy_url(uri):
                    return f"URI={uri}"
                absu = urljoin(base_url, uri)
                return f"URI={make_proxy_url(absu)}"

            try:
                line2 = RE_URI_QUOTED.sub(repl_quoted, line)
                line2 = RE_URI_UNQUOTED.sub(repl_unquoted, line2)
                out_lines.append(line2)
            except Exception:
                # Never break playlist if rewrite fails
                out_lines.append(line)
            continue

        # Non-comment URL line:
        if is_proxy_url(s):
            out_lines.append(line)
            continue

        absu = urljoin(base_url, s)
        out_lines.append(make_proxy_url(absu))

    # keep final newline (some clients are picky)
    return "\n".join(out_lines) + "\n"


# -----------------------------
# Routes (Must remain)
# -----------------------------
@app.route("/", methods=["GET"], endpoint="home")
def home() -> Response:
    # Serve player_v2.html if exists else player.html
    path = PLAYER_V2_PATH if os.path.exists(PLAYER_V2_PATH) else PLAYER_PATH
    if not os.path.exists(path):
        return Response("player.html / player_v2.html not found inside data folder", status=404)

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        html = f.read()

    return Response(
        html,
        content_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.route("/api/m3u/default", methods=["GET", "OPTIONS"], endpoint="m3u_default")
def m3u_default() -> Response:
    if request.method == "OPTIONS":
        return cors_preflight()

    if not os.path.exists(M3U_PATH):
        return Response("default_channels.m3u8 not found inside data folder", status=404)

    with open(M3U_PATH, "r", encoding="utf-8", errors="ignore") as f:
        data = f.read()

    return Response(
        data,
        content_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )


@app.route("/api/stream/probe", methods=["GET", "OPTIONS"], endpoint="probe")
def probe() -> Response:
    if request.method == "OPTIONS":
        return cors_preflight()

    url = (request.args.get("url", "") or "").strip()
    if not is_http_url(url):
        return make_response({"type": "hls", "ip_blocked": False, "error": "invalid url"}, 400)

    u = url.lower()
    if ".m3u8" in u:
        return make_response({"type": "hls", "ip_blocked": False}, 200)

    # Quick hints
    if any(x in u for x in (".ts", ".m4s", ".mp4", ".aac", ".mp3", ".vtt")):
        return make_response({"type": "ts", "ip_blocked": False}, 200)

    # Lightweight HEAD probe (supports redirects)
    try:
        sess = get_session()
        headers = build_upstream_headers(url)
        r = sess.head(url, headers=headers, allow_redirects=True, timeout=(4.0, 7.0))
        ct = (r.headers.get("content-type") or "").lower()
        if "mpegurl" in ct or ".m3u8" in (r.url or "").lower():
            return make_response({"type": "hls", "ip_blocked": False}, 200)
        return make_response({"type": "ts", "ip_blocked": False}, 200)
    except Exception:
        # fallback (safe default)
        return make_response({"type": "hls" if ".m3u8" in u else "ts", "ip_blocked": False}, 200)


@app.route("/api/stream/proxy", methods=["GET", "HEAD", "OPTIONS"], endpoint="stream_proxy")
def stream_proxy() -> Response:
    if request.method == "OPTIONS":
        return cors_preflight()

    url = (request.args.get("url", "") or "").strip()
    if not is_http_url(url):
        return Response("Invalid URL", status=400)

    sess = get_session()
    upstream_headers = build_upstream_headers(url)

    try:
        r = sess.request(
            method=request.method,
            url=url,
            headers=upstream_headers,
            stream=True,
            allow_redirects=True,
            timeout=UPSTREAM_TIMEOUT,
        )
    except Exception as e:
        return Response(f"Upstream error: {e}", status=502)

    content_type = r.headers.get("content-type", "")
    is_playlist = guess_is_m3u8(r.url, content_type)

    # Playlist rewrite
    if is_playlist:
        try:
            # requests will download and (if needed) decode compressed playlist for us
            raw = r.content
            text = raw.decode("utf-8", errors="ignore")
        except Exception:
            try:
                text = r.text
            except Exception:
                text = ""

        rewritten = rewrite_m3u8(text, r.url)
        headers = copy_response_headers(r, is_playlist=True)
        return Response(
            rewritten,
            status=r.status_code,
            content_type="application/vnd.apple.mpegurl",
            headers=headers,
        )

    # Binary streaming (TS/M4S/CMAF/KEY/VTT/etc.) - MUST NOT modify bytes
    try:
        # Prevent urllib3 auto-decompression so bytes remain identical to upstream body
        if hasattr(r, "raw") and hasattr(r.raw, "decode_content"):
            r.raw.decode_content = False
    except Exception:
        pass

    headers = copy_response_headers(r, is_playlist=False)
    # Keep upstream content-type when present
    if content_type:
        headers["Content-Type"] = content_type

    # HEAD: no body
    if request.method == "HEAD":
        resp = Response(status=r.status_code, headers=headers)
        try:
            r.close()
        except Exception:
            pass
        return resp

    def generate() -> Iterable[bytes]:
        try:
            # Prefer raw.stream for exact bytes
            if hasattr(r, "raw") and hasattr(r.raw, "stream"):
                for chunk in r.raw.stream(STREAM_CHUNK_SIZE, decode_content=False):
                    if chunk:
                        yield chunk
            else:
                for chunk in r.iter_content(chunk_size=STREAM_CHUNK_SIZE):
                    if chunk:
                        yield chunk
        except GeneratorExit:
            pass
        except Exception:
            pass
        finally:
            try:
                r.close()
            except Exception:
                pass

    return Response(
        stream_with_context(generate()),
        status=r.status_code,
        headers=headers,
        direct_passthrough=True,
    )


# -----------------------------
# Health Route (NEW)
# -----------------------------
@app.route("/health", methods=["GET", "OPTIONS"], endpoint="health")
def health() -> Response:
    if request.method == "OPTIONS":
        return cors_preflight()
    return make_response(
        {
            "status": "ok",
            "service": "iptv_proxy",
        },
        200,
    )


# -----------------------------
# Gunicorn / Render entry
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, threaded=True)            try:
                line2 = _RE_URI_QUOTED.sub(_repl_quoted, line)
                line2 = _RE_URI_UNQUOTED.sub(_repl_unquoted, line2)
                out_lines.append(line2)
            except Exception:
                # If rewrite fails, keep original line (never break playlist)
                out_lines.append(line)
            continue

        # Non-comment line: it's a URL (segment/variant/subtitle/etc.)
        # Keep already-proxied URLs intact to avoid double wrapping.
        if _is_proxy_url(s):
            out_lines.append(line)
            continue

        absu = urljoin(base_url, s)
        out_lines.append(make_proxy_url(absu))

    return "\n".join(out_lines) + "\n"

# -----------------------------
# Routes
# -----------------------------
@app.route("/", methods=["GET"])
def home():
    # Serve player_v2.html if present, else player.html, keeping the same route.
    path = PLAYER_V2_PATH if os.path.exists(PLAYER_V2_PATH) else PLAYER_PATH
    if not os.path.exists(path):
        return "player.html / player_v2.html not found inside data folder", 404

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        html = f.read()
    return Response(html, content_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.route("/player_v2", methods=["GET"])
def player_v2():
    if not os.path.exists(PLAYER_V2_PATH):
        return "player_v2.html not found", 404

    with open(PLAYER_V2_PATH, "r", encoding="utf-8", errors="ignore") as f:
        return Response(
            f.read(),
            content_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

@app.route("/api/m3u/default", methods=["GET","OPTIONS"])
def m3u_default():
    if request.method == "OPTIONS":
        return _cors_preflight_ok()

    if not os.path.exists(M3U_PATH):
        return "default_channels.m3u8 not found inside data folder", 404

    with open(M3U_PATH, "r", encoding="utf-8", errors="ignore") as f:
        data = f.read()

    return Response(
        data,
        content_type="application/vnd.apple.mpegurl",
        headers={
            "Cache-Control": "no-store",
            "Access-Control-Allow-Origin": "*",
        },
    )

@app.route("/api/stream/probe", methods=["GET", "OPTIONS"])
def probe():
    if request.method == "OPTIONS":
        return _cors_preflight_ok()

    url = request.args.get("url", "").strip()
    if not _is_http_url(url):
        return {"type": "hls", "ip_blocked": False, "error": "invalid url"}, 400

    # Fast heuristic first
    u = url.lower()
    if ".m3u8" in u:
        return {"type": "hls", "ip_blocked": False}
    if any(ext in u for ext in (".ts", ".m4s", ".mp4", ".aac")):
        return {"type": "ts", "ip_blocked": False}

    # Optional lightweight HEAD probe (keep fast)
    try:
        sess = _get_session()
        hdr = _build_upstream_headers(url, for_playlist_rewrite=False)
        r = sess.head(url, headers=hdr, allow_redirects=True, timeout=(4.0, 6.0))
        ct = (r.headers.get("content-type") or "").lower()
        if "mpegurl" in ct or "application/vnd.apple.mpegurl" in ct:
            return {"type": "hls", "ip_blocked": False}
        return {"type": "ts", "ip_blocked": False}
    except Exception:
        # Fallback
        return {"type": "ts" if not isProbablyHls(url) else "hls", "ip_blocked": False}

def isProbablyHls(url: str) -> bool:
    u = (url or "").lower()
    return ".m3u8" in u or "m3u8" in u

@app.route("/api/stream/proxy", methods=["GET", "HEAD", "OPTIONS"])
def proxy():
    if request.method == "OPTIONS":
        return _cors_preflight_ok()

    url = request.args.get("url", "").strip()
    if not _is_http_url(url):
        return "Invalid URL", 400

    sess = _get_session()

    # We'll decide playlist rewrite AFTER upstream response headers, but we may need
    # request header Accept-Encoding identity if it's a playlist to rewrite.
    # Strategy:
    #  - First request with for_playlist_rewrite=False (keeps client Accept-Encoding)
    #  - If it turns out to be m3u8 and Content-Encoding is present, we still can use r.text (requests decodes)
    #    BUT Content-Length could mismatch if we tried to stream. For m3u8 we don't stream; we rewrite in-memory.
    # So we can keep for_playlist_rewrite=False safely. Yet some servers require identity.
    # We'll prefer identity if URL suggests m3u8.
    likely_playlist = ".m3u8" in url.lower()

    upstream_headers = _build_upstream_headers(url, for_playlist_rewrite=likely_playlist)

    # Upstream request
    try:
        # stream=True for all; for playlists we will read text anyway (small)
        r = sess.request(
            method=request.method,
            url=url,
            headers=upstream_headers,
            stream=True,
            allow_redirects=True,
            timeout=_UPSTREAM_TIMEOUT,
        )
    except Exception as e:
        return f"Upstream error: {e}", 502

    # Determine type
    content_type = r.headers.get("content-type", "")
    is_playlist = _guess_is_m3u8(r.url, content_type)

    # If playlist: rewrite content safely
    if is_playlist:
        try:
            # requests will decode gzip/deflate automatically for .text/.content
            text = r.content.decode("utf-8", errors="ignore")
        except Exception:
            # last resort
            try:
                text = r.text
            except Exception:
                text = ""

        rewritten = rewrite_m3u8(text, r.url)

        resp_headers = {
            "Cache-Control": "no-store",
        }
        # Response headers copy (safe subset)
        resp_headers.update(_copy_response_headers(r, is_playlist=True))

        return Response(
            rewritten,
            status=r.status_code,
            content_type="application/vnd.apple.mpegurl",
            headers=resp_headers,
        )

    # Otherwise: stream bytes without buffering full file
    # Important: preserve raw bytes and Content-Encoding/Content-Length correctness
    try:
        # Avoid urllib3 auto-decompression while streaming raw
        # (so Content-Length/Content-Encoding remains consistent)
        if hasattr(r, "raw") and hasattr(r.raw, "decode_content"):
            r.raw.decode_content = False
    except Exception:
        pass

    resp_headers = _copy_response_headers(r, is_playlist=False)
    # Prefer no-store for IPTV live segments too (safe)
    resp_headers.setdefault("Cache-Control", "no-store")
    resp_headers["Access-Control-Allow-Origin"] = "*"

    def generate() -> Iterable[bytes]:
        try:
            # If raw stream exists, use it (preserves encoding)
            if hasattr(r, "raw") and hasattr(r.raw, "stream"):
                for chunk in r.raw.stream(_STREAM_CHUNK, decode_content=False):
                    if not chunk:
                        continue
                    yield chunk
                return

            # Fallback to iter_content
            for chunk in r.iter_content(chunk_size=_STREAM_CHUNK):
                if chunk:
                    yield chunk
        except GeneratorExit:
            # Client disconnected
            pass
        except Exception:
            # Upstream read error; end stream
            pass
        finally:
            try:
                r.close()
            except Exception:
                pass

    # For HEAD requests, no body
    if request.method == "HEAD":
        resp = Response(status=r.status_code, headers=resp_headers)
        # Ensure content-type if present
        if content_type:
            resp.headers["Content-Type"] = content_type
        return resp

    return Response(
        stream_with_context(generate()),
        status=r.status_code,
        headers=resp_headers,
        direct_passthrough=True,
    )

# -----------------------------
# Gunicorn/Render entry
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    # threaded=True helps with streaming while handling other requests in dev
    app.run(host="0.0.0.0", port=port, threaded=True)
