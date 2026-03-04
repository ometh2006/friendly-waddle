"""
Smart Video Compressor v10 — Hugging Face Spaces
- Any site supported by yt-dlp (YouTube, Twitter/X, Instagram, TikTok, etc.)
- GoFile share links handled via GoFile API (no rate limiting)
- Output: MP3 audio, 240p, 360p, 480p, 720p, 1080p
"""
import os, re, uuid, time, subprocess, threading, shutil, requests
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

app = FastAPI()
WORK_DIR = Path("/tmp/hf_compressed")
WORK_DIR.mkdir(exist_ok=True)

def _cleanup():
    while True:
        time.sleep(1800)
        now = time.time()
        for f in WORK_DIR.glob("*"):
            try:
                if now - f.stat().st_mtime > 3600:
                    shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
            except Exception:
                pass
threading.Thread(target=_cleanup, daemon=True).start()

VIDEO_PRESETS = {
    "240p":  {"scale": "426:240",   "bitrate": "200k",  "ab": "48k",  "crf": "34"},
    "360p":  {"scale": "640:360",   "bitrate": "400k",  "ab": "64k",  "crf": "32"},
    "480p":  {"scale": "854:480",   "bitrate": "800k",  "ab": "96k",  "crf": "30"},
    "720p":  {"scale": "1280:720",  "bitrate": "1600k", "ab": "128k", "crf": "28"},
    "1080p": {"scale": "1920:1080", "bitrate": "3500k", "ab": "192k", "crf": "26"},
}

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"

# ── GoFile website token ──────────────────────────────────────────────────────
_cached_wt = None
_cached_wt_time = 0
_WT_TTL = 1800

def _fetch_website_token() -> str:
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    pattern = re.compile(r'websiteToken\s*[=:]\s*["\']([a-zA-Z0-9]+)["\']')
    for url in ["https://gofile.io/dist/js/alljs.js",
                "https://gofile.io/dist/js/global.js",
                "https://gofile.io/dist/js/index.js"]:
        try:
            r = s.get(url, timeout=10)
            if r.status_code == 200:
                m = pattern.search(r.text)
                if m: return m.group(1)
        except Exception:
            continue
    try:
        home = s.get("https://gofile.io/", timeout=10)
        for js_path in re.findall(r'src=["\']([^"\']*\.js[^"\']*)["\']', home.text):
            js_url = js_path if js_path.startswith("http") else f"https://gofile.io{js_path}"
            try:
                jr = s.get(js_url, timeout=10)
                m = pattern.search(jr.text)
                if m: return m.group(1)
            except Exception:
                continue
    except Exception:
        pass
    return "4fd6sg89d7s6"

def get_website_token() -> str:
    global _cached_wt, _cached_wt_time
    if _cached_wt and (time.time() - _cached_wt_time) < _WT_TTL:
        return _cached_wt
    _cached_wt = _fetch_website_token()
    _cached_wt_time = time.time()
    return _cached_wt

def invalidate_website_token():
    global _cached_wt, _cached_wt_time
    _cached_wt = None
    _cached_wt_time = 0

def get_account_token() -> str:
    r = requests.post("https://api.gofile.io/accounts",
                      headers={"User-Agent": _UA}, timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("status") != "ok":
        raise RuntimeError(f"GoFile account error: {d}")
    return d["data"]["token"]

def _contents_api(cid, account_token, website_token):
    return requests.get(
        f"https://api.gofile.io/contents/{cid}",
        params={"token": account_token},
        headers={"User-Agent": _UA, "Referer": "https://gofile.io/",
                 "X-Website-Token": website_token},
        cookies={"accountToken": account_token},
        timeout=15,
    )

def resolve_share(url, account_token):
    m = re.search(r"gofile\.io/(?:d/|\?c=)([A-Za-z0-9]+)", url)
    if not m: raise ValueError("Cannot parse GoFile content ID.")
    cid = m.group(1)
    wt = get_website_token()
    r = _contents_api(cid, account_token, wt)
    if r.status_code == 401:
        invalidate_website_token()
        r = _contents_api(cid, account_token, get_website_token())
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"GoFile API: {data}")
    children = data["data"].get("children", {})
    for child in children.values():
        if child.get("type") == "file" and child.get("mimetype", "").startswith("video"):
            return child["link"], child.get("name", "video.mp4")
    for child in children.values():
        if child.get("type") == "file":
            return child["link"], child.get("name", "file")
    raise RuntimeError("No video found in GoFile share.")

def download_cdn(cdn_url, account_token, dest):
    with requests.get(cdn_url, stream=True, timeout=600,
                      headers={"User-Agent": _UA, "Referer": "https://gofile.io/",
                               "X-Website-Token": get_website_token()},
                      cookies={"accountToken": account_token}) as r:
        if r.status_code == 401: raise RuntimeError("GoFile 401: link expired or password-protected.")
        if r.status_code == 429: raise RuntimeError("GoFile 429: rate limited — wait a minute.")
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(512 * 1024):
                if chunk: f.write(chunk)

def download_gofile(url, job_dir, job_id):
    account_token = get_account_token()
    if re.search(r"gofile\.io/(?:d/|\?c=)", url):
        cdn_url, fname = resolve_share(url, account_token)
    else:
        fname = url.split("?")[0].rstrip("/").split("/")[-1] or "video.mp4"
        cdn_url = url
    safe = re.sub(r"[^\w._-]", "_", fname).strip("_") or f"video_{job_id}"
    dest = job_dir / f"src_{job_id}_{safe}"
    download_cdn(cdn_url, account_token, dest)
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError("Downloaded file is empty.")
    return dest

# ── yt-dlp download (YouTube, TikTok, Twitter, Instagram, etc.) ───────────────
def download_ytdlp(url, job_dir, job_id):
    tpl = str(job_dir / f"src_{job_id}.%(ext)s")
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--add-header", f"User-Agent:{_UA}",
        "-o", tpl,
        url,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp failed:\n{r.stderr[-2000:]}")
    for ext in ("mp4", "mkv", "mov", "webm", "avi"):
        c = job_dir / f"src_{job_id}.{ext}"
        if c.exists(): return c
    matches = list(job_dir.glob(f"src_{job_id}.*"))
    if matches: return matches[0]
    raise RuntimeError("yt-dlp ran but output file not found.")

def smart_download(url, job_dir, job_id):
    if "gofile.io" in url:
        return download_gofile(url, job_dir, job_id)
    return download_ytdlp(url, job_dir, job_id)

# ── Remux (fix moov atom on .mov / fragmented mp4) ────────────────────────────
def remux(src, job_dir):
    out = job_dir / f"{src.stem}_rx.mp4"
    r = subprocess.run([
        "ffmpeg", "-y", "-fflags", "+genpts",
        "-i", str(src), "-c", "copy", "-movflags", "+faststart", str(out),
    ], capture_output=True, text=True, timeout=300)
    if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
        return out
    return src

# ── FFmpeg encode video ───────────────────────────────────────────────────────
def encode_video(src, preset, out):
    p = VIDEO_PRESETS[preset]
    vf = (
        f"scale={p['scale']}:force_original_aspect_ratio=decrease,"
        f"pad={p['scale']}:(ow-iw)/2:(oh-ih)/2"
    )
    r = subprocess.run([
        "ffmpeg", "-y", "-i", src,
        "-vf", vf,
        "-c:v", "libx264", "-b:v", p["bitrate"], "-crf", p["crf"],
        "-preset", "veryfast",
        "-c:a", "aac", "-b:a", p["ab"],
        "-movflags", "+faststart",
        "-threads", "2",
        out,
    ], capture_output=True, text=True, timeout=7200)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-3000:])

# ── FFmpeg extract MP3 ────────────────────────────────────────────────────────
def encode_mp3(src, out):
    r = subprocess.run([
        "ffmpeg", "-y", "-i", src,
        "-vn",                  # no video
        "-c:a", "libmp3lame",
        "-b:a", "192k",
        "-q:a", "2",            # high quality VBR
        "-threads", "2",
        out,
    ], capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-3000:])

# ── API ───────────────────────────────────────────────────────────────────────
class CompressRequest(BaseModel):
    url:    str
    preset: str = "360p"  # "mp3" | "240p" | "360p" | "480p" | "720p" | "1080p"

class CompressResponse(BaseModel):
    job_id: str
    filename: str
    orig_mb: float
    comp_mb: float
    saved_pct: float
    elapsed_sec: float
    is_audio: bool

@app.post("/api/compress", response_model=CompressResponse)
def compress(req: CompressRequest):
    valid = list(VIDEO_PRESETS.keys()) + ["mp3"]
    if req.preset not in valid:
        raise HTTPException(400, f"Unknown preset. Choose: {valid}")
    url = req.url.strip()
    if not url:
        raise HTTPException(400, "URL required.")

    job_id  = uuid.uuid4().hex[:10]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    t0 = time.time()
    is_audio = req.preset == "mp3"

    try:
        try:
            src = smart_download(url, job_dir, job_id)
        except Exception as e:
            raise HTTPException(500, f"Download failed: {e}")

        orig_bytes = src.stat().st_size

        if is_audio:
            base = re.sub(r"[^\w._-]", "_",
                          re.sub(r"\.[^.]+$", "", src.name)).strip("_")[:60] or "audio"
            outname = f"{base}.mp3"
            outpath = str(WORK_DIR / outname)
            try:
                encode_mp3(str(src), outpath)
            except Exception as e:
                raise HTTPException(500, f"MP3 extraction failed: {e}")
        else:
            src = remux(src, job_dir)
            base = re.sub(r"[^\w._-]", "_",
                          re.sub(r"\.[^.]+$", "", src.stem)
                          ).strip("_").replace("_rx", "")[:60] or "video"
            outname = f"{base}_{req.preset}.mp4"
            outpath = str(WORK_DIR / outname)
            try:
                encode_video(str(src), req.preset, outpath)
            except Exception as e:
                raise HTTPException(500, f"Encoding failed: {e}")
    finally:
        shutil.rmtree(job_dir, ignore_errors=True)

    elapsed    = time.time() - t0
    comp_bytes = os.path.getsize(outpath)
    orig_mb    = orig_bytes / (1024 * 1024)
    comp_mb    = comp_bytes / (1024 * 1024)
    saved_pct  = round((1 - comp_mb / orig_mb) * 100, 1) if orig_mb else 0

    return CompressResponse(
        job_id=job_id, filename=outname,
        orig_mb=round(orig_mb, 2), comp_mb=round(comp_mb, 2),
        saved_pct=saved_pct, elapsed_sec=round(elapsed, 1),
        is_audio=is_audio,
    )

@app.get("/api/download/{filename}")
def download(filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid.")
    path = WORK_DIR / filename
    if not path.exists():
        raise HTTPException(404, "Expired.")
    media_type = "audio/mpeg" if filename.endswith(".mp3") else "video/mp4"
    return FileResponse(str(path), media_type=media_type, filename=filename,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Video & Audio Downloader</title>
<style>
  :root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3e;--accent:#6c63ff;--accent2:#a78bfa;
        --green:#22c55e;--red:#ef4444;--yellow:#f59e0b;--orange:#f97316;
        --text:#e2e8f0;--muted:#64748b;--radius:14px}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;
       min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:40px 16px 80px}
  .header{text-align:center;margin-bottom:36px}
  .header h1{font-size:clamp(1.8rem,4vw,2.5rem);font-weight:800;
    background:linear-gradient(135deg,var(--accent),var(--accent2));
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
  .header p{color:var(--muted);margin-top:8px;font-size:.93rem}
  .sites{display:flex;flex-wrap:wrap;gap:6px;justify-content:center;margin-top:14px}
  .site-tag{background:rgba(108,99,255,.1);border:1px solid rgba(108,99,255,.2);
    color:var(--accent2);font-size:.7rem;font-weight:600;padding:3px 10px;border-radius:99px}
  .card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
        padding:28px;width:100%;max-width:660px}
  label{display:block;font-size:.78rem;font-weight:600;color:var(--muted);
        text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px}
  input[type=text]{width:100%;background:var(--bg);border:1px solid var(--border);
    border-radius:10px;color:var(--text);font-size:.95rem;padding:12px 16px;
    outline:none;transition:border-color .2s}
  input[type=text]:focus{border-color:var(--accent)}
  input::placeholder{color:var(--muted)}
  .field{margin-bottom:20px}
  .url-hint{font-size:.74rem;margin-top:6px;min-height:18px}
  .url-hint.gofile{color:var(--green)}.url-hint.yt{color:var(--red)}
  .url-hint.social{color:var(--orange)}.url-hint.other{color:var(--muted)}

  /* Mode tabs */
  .mode-tabs{display:flex;gap:8px;margin-bottom:14px}
  .mode-tab{flex:1;padding:9px;border:1px solid var(--border);border-radius:10px;
    background:var(--bg);color:var(--muted);cursor:pointer;font-size:.85rem;font-weight:600;
    text-align:center;transition:all .2s}
  .mode-tab:hover{border-color:var(--accent)}
  .mode-tab.active{border-color:var(--accent);background:rgba(108,99,255,.12);color:var(--accent2)}

  /* Preset grid */
  .presets{display:grid;gap:8px;margin-bottom:22px}
  .presets.video-grid{grid-template-columns:repeat(5,1fr)}
  .presets.audio-grid{grid-template-columns:repeat(3,1fr)}
  .preset-btn{background:var(--bg);border:1px solid var(--border);border-radius:10px;
    color:var(--muted);cursor:pointer;padding:10px 6px;text-align:center;transition:all .2s;font-size:.82rem}
  .preset-btn .res{font-size:.95rem;font-weight:700;color:var(--text)}
  .preset-btn .save{font-size:.68rem;margin-top:2px}
  .preset-btn:hover{border-color:var(--accent)}
  .preset-btn.active{border-color:var(--accent);background:rgba(108,99,255,.12)}
  .preset-btn.active .res{color:var(--accent2)}
  .preset-btn.mp3-btn.active{border-color:var(--orange);background:rgba(249,115,22,.1)}
  .preset-btn.mp3-btn.active .res{color:var(--orange)}

  .btn{width:100%;background:linear-gradient(135deg,var(--accent),#8b5cf6);border:none;
    border-radius:10px;color:#fff;cursor:pointer;font-size:1rem;font-weight:700;padding:14px;
    transition:opacity .2s,transform .1s}
  .btn:hover:not(:disabled){opacity:.88;transform:translateY(-1px)}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .btn.audio-mode{background:linear-gradient(135deg,var(--orange),#ef4444)}

  .progress-wrap{display:none;margin-top:22px}
  .progress-label{display:flex;justify-content:space-between;font-size:.82rem;color:var(--muted);margin-bottom:8px}
  .progress-bar-bg{background:var(--bg);border-radius:99px;height:8px;overflow:hidden;border:1px solid var(--border)}
  .progress-bar{background:linear-gradient(90deg,var(--accent),var(--accent2));height:100%;
    width:0%;border-radius:99px;transition:width .4s ease}
  .progress-bar.audio{background:linear-gradient(90deg,var(--orange),#ef4444)}
  .spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(108,99,255,.3);
    border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;
    vertical-align:middle;margin-right:6px}
  @keyframes spin{to{transform:rotate(360deg)}}
  .warning-box{display:none;margin-top:14px;padding:11px 14px;border-radius:10px;font-size:.82rem;
    line-height:1.6;background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.2);color:#fcd34d}
  .result{display:none;margin-top:22px;background:var(--bg);border:1px solid var(--border);
    border-radius:12px;padding:20px}
  .result.success{border-color:rgba(34,197,94,.35)}.result.error{border-color:rgba(239,68,68,.35)}
  .result-title{font-weight:700;font-size:.95rem;margin-bottom:14px}
  .result.error .result-title{color:var(--red)}.result.success .result-title{color:var(--green)}
  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
  .stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;text-align:center}
  .stat-val{font-size:1.05rem;font-weight:700}.stat-val.green{color:var(--green)}
  .stat-label{font-size:.68rem;color:var(--muted);margin-top:3px;text-transform:uppercase;letter-spacing:.5px}
  .dl-btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;
    border-radius:10px;font-weight:700;font-size:.95rem;padding:12px;text-decoration:none;transition:background .2s}
  .dl-btn.video{background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.35);color:var(--green)}
  .dl-btn.video:hover{background:rgba(34,197,94,.22)}
  .dl-btn.audio{background:rgba(249,115,22,.12);border:1px solid rgba(249,115,22,.35);color:var(--orange)}
  .dl-btn.audio:hover{background:rgba(249,115,22,.22)}
  .error-msg{font-size:.8rem;color:var(--red);font-family:monospace;white-space:pre-wrap;
             word-break:break-word;line-height:1.6;max-height:200px;overflow-y:auto;
             background:rgba(239,68,68,.06);padding:12px;border-radius:8px}
  .supported{margin-top:28px;width:100%;max-width:660px}
  .supported h3{font-size:.75rem;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:10px}
  .site-list{display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px}
  .site-card{background:var(--card);border:1px solid var(--border);border-radius:10px;
    padding:10px 14px;font-size:.82rem;color:var(--muted);display:flex;align-items:center;gap:8px}
  .site-card b{color:var(--text)}
</style>
</head>
<body>

<div class="header">
  <h1>🎬 Video & Audio Downloader</h1>
  <p>Paste any video link — compress to any quality or extract MP3</p>
  <div class="sites">
    <span class="site-tag">YouTube</span>
    <span class="site-tag">GoFile</span>
    <span class="site-tag">TikTok</span>
    <span class="site-tag">Twitter/X</span>
    <span class="site-tag">Instagram</span>
    <span class="site-tag">Facebook</span>
    <span class="site-tag">Reddit</span>
    <span class="site-tag">Vimeo</span>
    <span class="site-tag">+ 1000 more</span>
  </div>
</div>

<div class="card">
  <div class="field">
    <label>Video or Share URL</label>
    <input type="text" id="urlInput" oninput="detectUrl(this.value)"
           placeholder="https://youtube.com/watch?v=… or gofile.io/d/…"/>
    <div class="url-hint other" id="urlHint">Paste any video URL above</div>
  </div>

  <!-- Mode tabs -->
  <label>Output Format</label>
  <div class="mode-tabs">
    <button class="mode-tab active" id="tabVideo" onclick="setMode('video')">🎬 Video</button>
    <button class="mode-tab" id="tabAudio" onclick="setMode('audio')">🎵 Audio (MP3)</button>
  </div>

  <!-- Video presets -->
  <div class="presets video-grid" id="videoPresets">
    <button class="preset-btn" data-preset="240p"><div class="res">240p</div><div class="save">~94% off</div></button>
    <button class="preset-btn active" data-preset="360p"><div class="res">360p</div><div class="save">~88% off</div></button>
    <button class="preset-btn" data-preset="480p"><div class="res">480p</div><div class="save">~75% off</div></button>
    <button class="preset-btn" data-preset="720p"><div class="res">720p</div><div class="save">~50% off</div></button>
    <button class="preset-btn" data-preset="1080p"><div class="res">1080p</div><div class="save">HD</div></button>
  </div>

  <!-- Audio presets -->
  <div class="presets audio-grid" id="audioPresets" style="display:none">
    <button class="preset-btn mp3-btn active" data-preset="mp3">
      <div class="res">🎵 MP3</div><div class="save">192kbps VBR</div>
    </button>
    <button class="preset-btn" data-preset="mp3" disabled style="opacity:.3;cursor:not-allowed">
      <div class="res">AAC</div><div class="save">coming soon</div>
    </button>
    <button class="preset-btn" data-preset="mp3" disabled style="opacity:.3;cursor:not-allowed">
      <div class="res">FLAC</div><div class="save">coming soon</div>
    </button>
  </div>

  <button class="btn" id="actionBtn" onclick="startCompress()">🚀 Download & Compress</button>

  <div class="warning-box" id="warningBox">
    ⏳ <b>Processing…</b> Don't close this page. May take a minute or two.
  </div>

  <div class="progress-wrap" id="progressWrap">
    <div class="progress-label">
      <span><span class="spinner"></span><span id="phaseText">Starting…</span></span>
      <span id="progressPct">0%</span>
    </div>
    <div class="progress-bar-bg">
      <div class="progress-bar" id="progressBar"></div>
    </div>
  </div>

  <div class="result" id="result">
    <div class="result-title" id="resultTitle"></div>
    <div id="resultBody"></div>
  </div>
</div>

<div class="supported">
  <h3>Supported sources</h3>
  <div class="site-list">
    <div class="site-card"><span>▶️</span><span><b>YouTube</b><br>videos, shorts</span></div>
    <div class="site-card"><span>☁️</span><span><b>GoFile</b><br>share links</span></div>
    <div class="site-card"><span>🎵</span><span><b>TikTok</b><br>videos</span></div>
    <div class="site-card"><span>𝕏</span><span><b>Twitter/X</b><br>videos</span></div>
    <div class="site-card"><span>📸</span><span><b>Instagram</b><br>reels, posts</span></div>
    <div class="site-card"><span>👥</span><span><b>Facebook</b><br>videos</span></div>
    <div class="site-card"><span>🎬</span><span><b>Vimeo</b><br>videos</span></div>
    <div class="site-card"><span>👾</span><span><b>Reddit</b><br>videos</span></div>
    <div class="site-card"><span>🌐</span><span><b>1000+ more</b><br>via yt-dlp</span></div>
  </div>
</div>

<script>
  let selectedPreset = '360p';
  let currentMode    = 'video';

  // Mode switch
  function setMode(mode) {
    currentMode = mode;
    document.getElementById('tabVideo').classList.toggle('active', mode==='video');
    document.getElementById('tabAudio').classList.toggle('active', mode==='audio');
    document.getElementById('videoPresets').style.display = mode==='video' ? 'grid' : 'none';
    document.getElementById('audioPresets').style.display = mode==='audio' ? 'grid' : 'none';
    const btn = document.getElementById('actionBtn');
    if (mode === 'audio') {
      selectedPreset = 'mp3';
      btn.textContent = '🎵 Extract MP3';
      btn.className = 'btn audio-mode';
      document.getElementById('progressBar').className = 'progress-bar audio';
    } else {
      selectedPreset = '360p';
      // re-select 360p button
      document.querySelectorAll('#videoPresets .preset-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.preset === '360p');
      });
      btn.textContent = '🚀 Download & Compress';
      btn.className = 'btn';
      document.getElementById('progressBar').className = 'progress-bar';
    }
  }

  // Video preset selection
  document.querySelectorAll('#videoPresets .preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('#videoPresets .preset-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      selectedPreset = btn.dataset.preset;
    });
  });

  // URL hint
  function detectUrl(v) {
    const h = document.getElementById('urlHint'); v = v.trim();
    if (!v) { h.className='url-hint other'; h.textContent='Paste any video URL above'; return; }
    if (/gofile\.io/.test(v))            { h.className='url-hint gofile';  h.textContent='☁️ GoFile — resolved via GoFile API'; }
    else if (/youtube\.com|youtu\.be/.test(v)) { h.className='url-hint yt'; h.textContent='▶️ YouTube — handled by yt-dlp'; }
    else if (/tiktok\.com/.test(v))      { h.className='url-hint social';  h.textContent='🎵 TikTok — handled by yt-dlp'; }
    else if (/twitter\.com|x\.com/.test(v)) { h.className='url-hint social'; h.textContent='𝕏 Twitter/X — handled by yt-dlp'; }
    else if (/instagram\.com/.test(v))   { h.className='url-hint social';  h.textContent='📸 Instagram — handled by yt-dlp'; }
    else if (/facebook\.com|fb\.watch/.test(v)) { h.className='url-hint social'; h.textContent='👥 Facebook — handled by yt-dlp'; }
    else if (/vimeo\.com/.test(v))       { h.className='url-hint social';  h.textContent='🎬 Vimeo — handled by yt-dlp'; }
    else                                 { h.className='url-hint other';   h.textContent='🌐 Unknown site — trying yt-dlp'; }
  }

  // Progress
  let pInt=null, pct=0, pi=0;
  const VIDEO_PH = [
    {l:'📥 Downloading…',        e:40, s:1.0},
    {l:'🔧 Remuxing container…', e:48, s:1.5},
    {l:'⚙️ Encoding video…',    e:95, s:0.55},
  ];
  const AUDIO_PH = [
    {l:'📥 Downloading…',        e:50, s:1.2},
    {l:'🎵 Extracting MP3…',     e:95, s:0.9},
  ];

  function startP() {
    pct=0; pi=0;
    const PH = currentMode==='audio' ? AUDIO_PH : VIDEO_PH;
    const bar=document.getElementById('progressBar');
    const pEl=document.getElementById('progressPct');
    const tEl=document.getElementById('phaseText');
    pInt=setInterval(()=>{
      const p=PH[Math.min(pi,PH.length-1)];
      if(pct<p.e){pct+=p.s;}else if(pi<PH.length-1){pi++;}
      bar.style.width=Math.min(pct,95)+'%';
      pEl.textContent=Math.floor(Math.min(pct,95))+'%';
      tEl.textContent=PH[Math.min(pi,PH.length-1)].l;
    },400);
  }

  function stopP(f=100){
    clearInterval(pInt);
    document.getElementById('progressBar').style.width=f+'%';
    document.getElementById('progressPct').textContent=f+'%';
    document.getElementById('phaseText').textContent=f===100?'✅ Done!':'❌ Failed';
  }

  async function startCompress() {
    const url = document.getElementById('urlInput').value.trim();
    if (!url) { showErr('Please paste a video URL above.'); return; }
    const btn = document.getElementById('actionBtn');
    btn.disabled=true;
    btn.textContent='⚙️ Processing…';
    document.getElementById('progressWrap').style.display='block';
    document.getElementById('warningBox').style.display='block';
    document.getElementById('result').style.display='none';
    startP();
    try {
      const res = await fetch('/api/compress', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({url, preset: selectedPreset}),
      });
      const d = await res.json();
      stopP(res.ok?100:0);
      document.getElementById('warningBox').style.display='none';
      res.ok ? showOk(d) : showErr(d.detail||'Failed.');
    } catch(e) {
      stopP(0);
      document.getElementById('warningBox').style.display='none';
      showErr('Network error: '+e.message);
    } finally {
      btn.disabled=false;
      btn.textContent = currentMode==='audio' ? '🎵 Extract MP3' : '🚀 Download & Compress';
    }
  }

  function showOk(d) {
    const el=document.getElementById('result');
    el.className='result success'; el.style.display='block';
    document.getElementById('resultTitle').textContent=
      (d.is_audio ? '🎵' : '✅') + ' Done in '+d.elapsed_sec+'s';
    const dlClass = d.is_audio ? 'audio' : 'video';
    const dlIcon  = d.is_audio ? '⬇️ Download MP3' : '⬇️ Download Video';
    document.getElementById('resultBody').innerHTML=`
      <div class="stats">
        <div class="stat"><div class="stat-val">${d.orig_mb} MB</div><div class="stat-label">Original</div></div>
        <div class="stat"><div class="stat-val">${d.comp_mb} MB</div><div class="stat-label">Output</div></div>
        <div class="stat"><div class="stat-val green">${d.saved_pct > 0 ? d.saved_pct+'% off' : 'Audio'}</div><div class="stat-label">Saved</div></div>
      </div>
      <a class="dl-btn ${dlClass}" href="/api/download/${encodeURIComponent(d.filename)}" download>
        ${dlIcon} — ${d.filename}
      </a>`;
  }

  function showErr(m) {
    const el=document.getElementById('result');
    el.className='result error'; el.style.display='block';
    document.getElementById('resultTitle').textContent='❌ Error';
    document.getElementById('resultBody').innerHTML=
      `<div class="error-msg">${m.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>`;
  }

  document.getElementById('urlInput').addEventListener('keydown', e=>{
    if(e.key==='Enter') startCompress();
  });
</script>
</body>
</html>"""
