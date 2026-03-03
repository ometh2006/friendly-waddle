"""
Smart Video Compressor — FastAPI backend
Streams video URL directly into FFmpeg. HD file never saved to disk.
"""
import os, re, uuid, time, subprocess, threading, requests
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

app = FastAPI(title="Smart Video Compressor")

WORK_DIR = Path("/tmp/compressed")
WORK_DIR.mkdir(exist_ok=True)

# ── cleanup old files every 30 min ──────────────────────────────────────────
def _cleanup():
    while True:
        time.sleep(1800)
        now = time.time()
        for f in WORK_DIR.glob("*.mp4"):
            if now - f.stat().st_mtime > 3600:
                f.unlink(missing_ok=True)

threading.Thread(target=_cleanup, daemon=True).start()

# ── Presets ──────────────────────────────────────────────────────────────────
PRESETS = {
    "240p": {"scale": "426:240",  "bitrate": "180k", "ab": "48k",  "crf": "32"},
    "360p": {"scale": "640:360",  "bitrate": "350k", "ab": "64k",  "crf": "30"},
    "480p": {"scale": "854:480",  "bitrate": "700k", "ab": "96k",  "crf": "28"},
    "720p": {"scale": "1280:720", "bitrate": "1500k","ab": "128k", "crf": "24"},
}

_UA      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"
_REFERER = "https://gofile.io/"
_sess    = requests.Session()
_sess.headers.update({"User-Agent": _UA, "Referer": _REFERER})

# ── GoFile API ────────────────────────────────────────────────────────────────
def _gofile_token() -> str:
    r = _sess.post("https://api.gofile.io/accounts", timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("status") != "ok":
        raise RuntimeError(f"GoFile token error: {d}")
    return d["data"]["token"]

def resolve_url(raw: str):
    """Returns (direct_url, filename, size_bytes, token_or_None)"""
    if "gofile.io/d/" in raw or "gofile.io/?c=" in raw:
        m = re.search(r"gofile\.io/(?:d/|\?c=)([A-Za-z0-9]+)", raw)
        if not m:
            raise ValueError("Cannot parse GoFile content ID.")
        cid = m.group(1)
        tok = _gofile_token()
        _sess.cookies.set("accountToken", tok)
        r = _sess.get(
            f"https://api.gofile.io/contents/{cid}",
            params={"token": tok, "wt": "4fd6sg89d7s6"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            raise RuntimeError(f"GoFile API error: {data}")
        for child in data["data"].get("children", {}).values():
            if child.get("type") == "file" and child.get("mimetype", "").startswith("video"):
                return child["link"], child.get("name", "video.mp4"), child.get("size", 0), tok
        raise RuntimeError("No video found in this GoFile share.")
    else:
        fname = raw.split("?")[0].rstrip("/").split("/")[-1] or "video.mp4"
        try:
            h    = _sess.head(raw, timeout=10, allow_redirects=True)
            size = int(h.headers.get("content-length", 0))
        except Exception:
            size = 0
        return raw, fname, size, None

# ── FFmpeg encoder (URL → compressed file, no HD on disk) ────────────────────
def encode(direct_url: str, preset: str, output_path: str, token: str = None):
    p  = PRESETS[preset]
    vf = (
        f"scale={p['scale']}:force_original_aspect_ratio=decrease,"
        f"pad={p['scale']}:(ow-iw)/2:(oh-ih)/2"
    )
    headers_str = f"User-Agent: {_UA}\r\nReferer: {_REFERER}\r\n"
    if token:
        headers_str += f"Cookie: accountToken={token}\r\n"

    cmd = [
        "ffmpeg", "-y",
        "-headers", headers_str,
        "-i", direct_url,
        "-vf", vf,
        "-c:v", "libx264", "-b:v", p["bitrate"], "-crf", p["crf"],
        "-c:a", "aac",     "-b:a", p["ab"],
        "-preset", "veryfast",
        "-movflags", "+faststart",
        "-threads", "2",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr[-3000:])

# ── API models ────────────────────────────────────────────────────────────────
class CompressRequest(BaseModel):
    url:    str
    preset: str = "360p"

class CompressResponse(BaseModel):
    job_id:      str
    filename:    str
    orig_mb:     float
    comp_mb:     float
    saved_pct:   float
    elapsed_sec: float

# ── Routes ────────────────────────────────────────────────────────────────────
@app.post("/api/compress", response_model=CompressResponse)
def compress(req: CompressRequest):
    if req.preset not in PRESETS:
        raise HTTPException(400, f"Unknown preset. Choose from: {list(PRESETS)}")

    url = req.url.strip()
    if not url:
        raise HTTPException(400, "URL is required.")

    # Resolve
    try:
        direct_url, fname, orig_bytes, token = resolve_url(url)
    except Exception as e:
        raise HTTPException(400, f"URL resolve error: {e}")

    # Prepare output
    job_id  = uuid.uuid4().hex[:10]
    base    = re.sub(r"\.[^.]+$", "", fname)
    base    = re.sub(r"[^\w._-]", "_", base).strip("_") or "video"
    outname = f"{base}_{req.preset}_{job_id}.mp4"
    outpath = str(WORK_DIR / outname)

    # Encode
    t0 = time.time()
    try:
        encode(direct_url, req.preset, outpath, token=token)
    except Exception as e:
        raise HTTPException(500, f"Encoding failed: {e}")

    elapsed    = time.time() - t0
    comp_bytes = os.path.getsize(outpath)
    orig_mb    = orig_bytes / (1024 * 1024) if orig_bytes else comp_bytes / (1024 * 1024)
    comp_mb    = comp_bytes / (1024 * 1024)
    saved_pct  = round((1 - comp_mb / orig_mb) * 100, 1) if orig_mb else 0

    return CompressResponse(
        job_id      = job_id,
        filename    = outname,
        orig_mb     = round(orig_mb, 2),
        comp_mb     = round(comp_mb, 2),
        saved_pct   = saved_pct,
        elapsed_sec = round(elapsed, 1),
    )


@app.get("/api/download/{filename}")
def download(filename: str):
    # Sanitise — no path traversal
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename.")
    path = WORK_DIR / filename
    if not path.exists():
        raise HTTPException(404, "File not found or expired.")
    return FileResponse(
        path        = str(path),
        media_type  = "video/mp4",
        filename    = filename,
        headers     = {"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/presets")
def presets():
    return {
        k: {**v, "label": k, "savings": {
            "240p": "~94%", "360p": "~88%", "480p": "~75%", "720p": "~50%"
        }.get(k, "")}
        for k, v in PRESETS.items()
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ── Serve frontend ────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    return open("static/index.html").read()
