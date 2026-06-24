from flask import Flask, request, Response
import requests, os, re
from urllib.parse import urljoin, quote

app = Flask(__name__)
BASE_DIR = os.path.dirname(__file__)
DATA_DIR = os.path.join(BASE_DIR, "data")
M3U_PATH = os.path.join(DATA_DIR, "default_channels.m3u8")
PLAYER_PATH = os.path.join(DATA_DIR, "player.html")

HEADERS = {
    "User-Agent": "VLC/3.0.20 LibVLC/3.0.20",
    "Accept": "*/*",
    "Connection": "keep-alive"
}

@app.route("/")
def home():
    if not os.path.exists(PLAYER_PATH):
        return "player.html not found inside data folder", 404
    with open(PLAYER_PATH, "r", encoding="utf-8", errors="ignore") as f:
        return Response(f.read(), content_type="text/html; charset=utf-8")

@app.route("/api/m3u/default")
def m3u_default():
    if not os.path.exists(M3U_PATH):
        return "default_channels.m3u8 not found inside data folder", 404
    with open(M3U_PATH, "r", encoding="utf-8", errors="ignore") as f:
        return Response(f.read(), content_type="application/vnd.apple.mpegurl",
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})

@app.route("/api/stream/probe")
def probe():
    url = request.args.get("url", "")
    if not url.startswith(("http://", "https://")):
        return {"type": "hls", "ip_blocked": False, "error": "invalid url"}, 400
    return {"type": "hls" if ".m3u8" in url.lower() else "ts", "ip_blocked": False}

def make_proxy_url(u):
    return "/api/stream/proxy?url=" + quote(u, safe="")

def rewrite_m3u8(text, base_url):
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            lines.append(line); continue
        upper = s.upper()
        if upper.startswith("#EXT-X-ENDLIST") or upper.startswith("#EXT-X-PLAYLIST-TYPE"):
            continue
        if s.startswith("#"):
            def repl(m):
                return 'URI="' + make_proxy_url(urljoin(base_url, m.group(1))) + '"'
            lines.append(re.sub(r'URI="([^"]+)"', repl, line))
        else:
            lines.append(make_proxy_url(urljoin(base_url, s)))
    return "\n".join(lines)

@app.route("/api/stream/proxy")
def proxy():
    url = request.args.get("url", "")
    if not url.startswith(("http://", "https://")):
        return "Invalid URL", 400
    try:
        r = requests.get(url, headers=HEADERS, stream=True, timeout=30)
    except Exception as e:
        return f"Upstream error: {e}", 502
    content_type = r.headers.get("content-type", "")
    if ".m3u8" in url.lower() or "mpegurl" in content_type.lower():
        text = r.content.decode("utf-8", errors="ignore")
        return Response(rewrite_m3u8(text, r.url), content_type="application/vnd.apple.mpegurl",
                        headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})
    def generate():
        try:
            for chunk in r.iter_content(chunk_size=256 * 1024):
                if chunk:
                    yield chunk
        except Exception:
            pass
    return Response(generate(), status=r.status_code,
                    content_type=content_type or "application/octet-stream",
                    headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port)
