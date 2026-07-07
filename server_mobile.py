from __future__ import annotations

import os
import re
import time
import threading
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urljoin, quote, urlparse

import requests
from flask import Flask, Response, request, stream_with_context, make_response

# -----------------------------
# App / Paths
# -----------------------------
app = Flask(__name__)

BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
M3U_PATH = os.path.join(DATA_DIR, "default_channels.m3u8")

# Keep compatibility: serve player_v2.html if exists, else player.html
PLAYER_V2_PATH = os.path.join(DATA_DIR, "player_v2.html")
PLAYER_PATH = os.path.join(DATA_DIR, "player.html")

# -----------------------------
# HTTP Session Pool (thread-local)
# -----------------------------
_DEFAULT_UA = "VLC/3.0.20 LibVLC/3.0.20"

# timeouts: (connect, read)
_UPSTREAM_TIMEOUT: Tuple[float, float] = (8.0, 30.0)
_STREAM_CHUNK = 256 * 1024

# We keep a per-thread session (safe under gunicorn threads) while still pooling connections.
_tls = threading.local()

def _get_session() -> requests.Session:
    sess = getattr(_tls, "session", None)
    if sess is None:
        sess = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=100,
            pool_maxsize=200,
            max_retries=0,  # We implement retry logic at player side; proxy should be deterministic.
            pool_block=False,
        )
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
        _tls.session = sess
    return sess

# -----------------------------
# CORS (global)
# -----------------------------
_CORS_ALLOW_HEADERS = (
    "Origin, Referer, User-Agent, Content-Type, Accept, Accept-Encoding, "
    "Range, Cache-Control, If-None-Match, If-Modified-Since, "
    "X-Requested-With"
)
_CORS_ALLOW_METHODS = "GET, HEAD, OPTIONS"

@app.after_request
def add_cors_headers(resp: Response):
    # CORS: allow any origin (typical for IPTV player usage)
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = _CORS_ALLOW_METHODS
    resp.headers["Access-Control-Allow-Headers"] = _CORS_ALLOW_HEADERS
    resp.headers["Access-Control-Expose-Headers"] = (
        "Content-Length, Content-Range, Accept-Ranges, ETag, Last-Modified, Cache-Control"
    )
    # Help streaming in some reverse proxies
    resp.headers["X-Accel-Buffering"] = "no"
    return resp

def _cors_preflight_ok() -> Response:
    resp = make_response("", 204)
    return resp

# -----------------------------
# Helpers
# -----------------------------
def make_proxy_url(u: str) -> str:
    # Encode fully (safe="") so upstream query remains intact within url=
    return "/api/stream/proxy?url=" + quote(u, safe="")

def _is_http_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith(("http://", "https://"))

def _is_proxy_url(url: str) -> bool:
    return isinstance(url, str) and url.startswith("/api/stream/proxy?url=")

def _guess_is_m3u8(url: str, content_type: str) -> bool:
    u = (url or "").lower()
    ct = (content_type or "").lower()
    if ".m3u8" in u:
        return True
    if "application/vnd.apple.mpegurl" in ct or "application/x-mpegurl" in ct:
        return True
    if "mpegurl" in ct:
        return True
    # Sometimes servers return text/plain for m3u8
    if ct.startswith("text/") and ".m3u8" in u:
        return True
    return False

def _safe_header_get(name: str) -> Optional[str]:
    v = request.headers.get(name)
    if v is None:
        return None
    v = str(v).strip()
    return v if v else None

def _build_upstream_headers(upstream_url: str, for_playlist_rewrite: bool) -> Dict[str, str]:
    """
    Forward key headers as required. For playlist rewrite requests, we prefer Accept-Encoding=identity
    to safely rewrite text (avoid compressed transfer mismatches).
    """
    hdr: Dict[str, str] = {}

    # Required forwards
    ua = _safe_header_get("User-Agent") or _DEFAULT_UA
    hdr["User-Agent"] = ua

    for h in ("Referer", "Origin", "Range", "Accept", "Connection", "Cache-Control",
              "If-None-Match", "If-Modified-Since"):
        v = _safe_header_get(h)
        if v:
            hdr[h] = v

    # Accept-Encoding: keep for binary streaming; for playlists we ask identity for correctness
    ae = _safe_header_get("Accept-Encoding")
    if for_playlist_rewrite:
        hdr["Accept-Encoding"] = "identity"
    elif ae:
        hdr["Accept-Encoding"] = ae

    # Host: set to upstream host (do not forward our server host)
    try:
        parsed = urlparse(upstream_url)
        if parsed.netloc:
            hdr["Host"] = parsed.netloc
    except Exception:
        pass

    return hdr

def _copy_response_headers(up: requests.Response, is_playlist: bool) -> Dict[str, str]:
    """
    Copy important headers. Avoid hop-by-hop headers.
    For rewritten playlists, Content-Length will be set by Flask automatically.
    """
    keep = {
        "Content-Type",
        "Content-Length",
        "Accept-Ranges",
        "Content-Range",
        "ETag",
        "Last-Modified",
        "Cache-Control",
        "Content-Encoding",  # If we stream raw bytes, this matters
    }
    out: Dict[str, str] = {}
    for k, v in up.headers.items():
        kk = k.title()
        if kk in keep and v is not None:
            out[kk] = v

    # Ensure no caching for playlists unless upstream explicitly wants it; IPTV live prefers no-store
    if is_playlist:
        out["Cache-Control"] = "no-store"
        out["Content-Type"] = "application/vnd.apple.mpegurl"

    return out

# -----------------------------
# M3U8 Rewriter
# -----------------------------
# Rewrite URI="..." (quoted)
_RE_URI_QUOTED = re.compile(r'URI="([^"]+)"')
# Rewrite URI=... (unquoted) until comma/space/end
_RE_URI_UNQUOTED = re.compile(r'\bURI=([^",\s]+)')

def rewrite_m3u8(text: str, base_url: str) -> str:
    """
    Rewrite:
      - all non-comment lines (segments/variant playlists/subtitles) to proxy URLs
      - URI attributes in tags: EXT-X-KEY, EXT-X-MAP, EXT-X-MEDIA, EXT-X-I-FRAME-STREAM-INF, etc.
      - Works for LL-HLS tags too (PRELOAD-HINT, RENDITION-REPORT, PART with URI attrs)
    Must not break m3u8 formatting; preserve lines and comments.
    """
    out_lines = []
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        s = line.strip()

        if not s:
            out_lines.append(line)
            continue

        if s.startswith("#"):
            # Rewrite URI="..."/URI=... within tag lines
            def _repl_quoted(m: re.Match) -> str:
                uri = m.group(1)
                if _is_proxy_url(uri):
                    return f'URI="{uri}"'
                absu = urljoin(base_url, uri)
                return f'URI="{make_proxy_url(absu)}"'

            def _repl_unquoted(m: re.Match) -> str:
                uri = m.group(1)
                if _is_proxy_url(uri):
                    return f"URI={uri}"
                absu = urljoin(base_url, uri)
                return f"URI={make_proxy_url(absu)}"

            try:
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

@app.route("/", methods=["GET"])
def home():
    path = PLAYER_V2_PATH if os.path.exists(PLAYER_V2_PATH) else PLAYER_PATH

    if not os.path.exists(path):
        return "player.html / player_v2.html not found inside data folder", 404

    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        html = f.read()

    return Response(
        html,
        content_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


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

@app.route("/api/m3u/default", methods=["GET", "OPTIONS"])
def m3u_default():
    if request.method == "OPTIONS":
        return _cors_preflight_ok()

    if not os.path.exists(M3U_PATH):
        return "default_channels.m3u8 not found inside data folder", 404

    with open(M3U_PATH, "r", encoding="utf-8", errors="ignore") as f:
        data = f.read()

    # Return playlist as-is (client will proxy segments through /api/stream/proxy)
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
