"""
Smart Video Compressor v5
GoFile handled via GoFile API only. yt-dlp only for non-GoFile URLs.
"""
import os, re, uuid, time, subprocess, threading, shutil, requests
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

app = FastAPI()
WORK_DIR = Path("/tmp/compressed")
WORK_DIR.mkdir(exist_ok=True)

def _cleanup():
    while True:
        time.sleep(1800)
        now = time.time()
        for f in WORK_DIR.glob("*"):
            try:
                if now - f.stat().st_mtime > 3600:
                    shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
            except: pass
threading.Thread(target=_cleanup, daemon=True).start()

PRESETS = {
    "240p": {"scale":"426:240",  "bitrate":"180k",  "ab":"48k",  "crf":"32"},
    "360p": {"scale":"640:360",  "bitrate":"350k",  "ab":"64k",  "crf":"30"},
    "480p": {"scale":"854:480",  "bitrate":"700k",  "ab":"96k",  "crf":"28"},
    "720p": {"scale":"1280:720", "bitrate":"1500k", "ab":"128k", "crf":"24"},
}
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"

# ── GoFile API ────────────────────────────────────────────────────────────────
def _token():
    r = requests.post("https://api.gofile.io/accounts", timeout=15,
                      headers={"User-Agent": _UA})
    r.raise_for_status()
    d = r.json()
    if d.get("status") != "ok": raise RuntimeError(f"Token error: {d}")
    return d["data"]["token"]

def _resolve_share(url, token):
    m = re.search(r"gofile\.io/(?:d/|\?c=)([A-Za-z0-9]+)", url)
    if not m: raise ValueError("Cannot parse GoFile content ID.")
    r = requests.get(
        f"https://api.gofile.io/contents/{m.group(1)}",
        params={"token": token, "wt": "4fd6sg89d7s6"},
        headers={"User-Agent": _UA, "Referer": "https://gofile.io/"},
        cookies={"accountToken": token}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok": raise RuntimeError(f"GoFile API: {data}")
    for child in data["data"].get("children", {}).values():
        if child.get("type") == "file" and child.get("mimetype","").startswith("video"):
            return child["link"], child.get("name","video.mp4")
    for child in data["data"].get("children", {}).values():
        if child.get("type") == "file":
            return child["link"], child.get("name","file")
    raise RuntimeError("No video found in this GoFile share.")

def _dl_cdn(cdn_url, token, dest):
    with requests.get(cdn_url, stream=True, timeout=600,
                      headers={"User-Agent":_UA,"Referer":"https://gofile.io/"},
                      cookies={"accountToken": token}) as r:
        if r.status_code == 401: raise RuntimeError("GoFile 401: File may be password-protected or link expired.")
        if r.status_code == 429: raise RuntimeError("GoFile 429: Rate limited. Wait a minute and try again.")
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(1024*1024):
                if chunk: f.write(chunk)

def download_gofile(url, job_dir, job_id):
    tok = _token()
    if re.search(r"gofile\.io/(?:d/|\?c=)", url):
        cdn_url, fname = _resolve_share(url, tok)
    else:
        fname = url.split("?")[0].rstrip("/").split("/")[-1] or "video.mp4"
        cdn_url = url
    safe = re.sub(r"[^\w._-]", "_", fname).strip("_") or f"video_{job_id}"
    dest = job_dir / f"src_{job_id}_{safe}"
    _dl_cdn(cdn_url, tok, dest)
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError("Downloaded file is empty.")
    return dest

def download_ytdlp(url, job_dir, job_id):
    tpl = str(job_dir / f"src_{job_id}.%(ext)s")
    r = subprocess.run(["yt-dlp","--no-playlist","--no-warnings",
        "-f","bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
        "--merge-output-format","mp4","-o",tpl,url],
        capture_output=True, text=True, timeout=600)
    if r.returncode != 0: raise RuntimeError(f"yt-dlp: {r.stderr[-2000:]}")
    for ext in ("mp4","mkv","mov","webm"):
        c = job_dir / f"src_{job_id}.{ext}"
        if c.exists(): return c
    m = list(job_dir.glob(f"src_{job_id}.*"))
    if m: return m[0]
    raise RuntimeError("yt-dlp output not found.")

def smart_download(url, job_dir, job_id):
    if "gofile.io" in url: return download_gofile(url, job_dir, job_id)
    return download_ytdlp(url, job_dir, job_id)

def encode(src, preset, out):
    p = PRESETS[preset]
    vf = f"scale={p['scale']}:force_original_aspect_ratio=decrease,pad={p['scale']}:(ow-iw)/2:(oh-ih)/2"
    r = subprocess.run(["ffmpeg","-y","-i",src,"-vf",vf,
        "-c:v","libx264","-b:v",p["bitrate"],"-crf",p["crf"],
        "-c:a","aac","-b:a",p["ab"],
        "-preset","veryfast","-movflags","+faststart","-threads","2",out],
        capture_output=True, text=True, timeout=3600)
    if r.returncode != 0: raise RuntimeError(r.stderr[-3000:])

class CompressRequest(BaseModel):
    url: str
    preset: str = "360p"

class CompressResponse(BaseModel):
    job_id: str; filename: str; orig_mb: float
    comp_mb: float; saved_pct: float; elapsed_sec: float

@app.post("/api/compress", response_model=CompressResponse)
def compress(req: CompressRequest):
    if req.preset not in PRESETS: raise HTTPException(400, f"Unknown preset: {list(PRESETS)}")
    url = req.url.strip()
    if not url: raise HTTPException(400, "URL required.")
    job_id = uuid.uuid4().hex[:10]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    t0 = time.time()
    try:
        try: src = smart_download(url, job_dir, job_id)
        except Exception as e: raise HTTPException(500, f"Download failed: {e}")
        orig_bytes = src.stat().st_size
        base = re.sub(r"[^\w._-]","_", re.sub(r"\.[^.]+$","",src.name)).strip("_")[:60] or "video"
        outname = f"{base}_{req.preset}.mp4"
        outpath = str(WORK_DIR / outname)
        try: encode(str(src), req.preset, outpath)
        except Exception as e: raise HTTPException(500, f"Encoding failed: {e}")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)
    elapsed = time.time() - t0
    comp_bytes = os.path.getsize(outpath)
    orig_mb = orig_bytes / (1024*1024)
    comp_mb = comp_bytes / (1024*1024)
    return CompressResponse(job_id=job_id, filename=outname,
        orig_mb=round(orig_mb,2), comp_mb=round(comp_mb,2),
        saved_pct=round((1-comp_mb/orig_mb)*100,1) if orig_mb else 0,
        elapsed_sec=round(elapsed,1))

@app.get("/api/download/{filename}")
def download(filename: str):
    if "/" in filename or ".." in filename: raise HTTPException(400,"Invalid.")
    path = WORK_DIR / filename
    if not path.exists(): raise HTTPException(404,"Expired.")
    return FileResponse(str(path), media_type="video/mp4", filename=filename)

@app.get("/health")
def health(): return {"status":"ok"}

@app.get("/", response_class=HTMLResponse)
def index(): return open("index.html").read()
```

And create `index.html` as a separate file in your repo with the frontend from v4 (or the previous version). Your repo will then have:
```
your-repo/
├── app.py            ← above code
├── index.html        ← copy from previous version
├── Dockerfile        ← downloaded above
└── requirements.txt
