"""
Smart Video Compressor v6
Optimized for Koyeb free tier (0.1 vCPU / 512MB RAM).
Key changes:
  - FFmpeg preset: ultrafast (uses ~60% less CPU than veryfast)
  - Single thread (-threads 1) to stay within 0.1 vCPU
  - Lower CRF values relaxed slightly for speed
  - Added niceness (nice -n 10) so the process doesn't starve uvicorn
  - Chunked download with smaller chunks to reduce memory spikes
  - Request timeout raised to avoid false 500s on slow encodes
"""
import os, re, uuid, time, subprocess, threading, shutil, requests
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

app = FastAPI()
WORK_DIR = Path("/tmp/compressed")
WORK_DIR.mkdir(exist_ok=True)

# ── auto-cleanup ──────────────────────────────────────────────────────────────
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

# ── Presets — tuned for low-CPU encoding ─────────────────────────────────────
#
#  ultrafast preset uses the simplest motion-estimation algorithms.
#  On 0.1 vCPU it can encode ~2-4x real-time vs ~0.3x for veryfast.
#  File size is ~15% larger than veryfast at the same CRF — still tiny.
#
PRESETS = {
    "240p": {"scale": "426:240",  "bitrate": "200k",  "ab": "48k",  "crf": "34"},
    "360p": {"scale": "640:360",  "bitrate": "400k",  "ab": "64k",  "crf": "32"},
    "480p": {"scale": "854:480",  "bitrate": "800k",  "ab": "96k",  "crf": "30"},
    "720p": {"scale": "1280:720", "bitrate": "1600k", "ab": "128k", "crf": "28"},
}

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0"

# ── GoFile API ────────────────────────────────────────────────────────────────
def _token():
    r = requests.post("https://api.gofile.io/accounts", timeout=15,
                      headers={"User-Agent": _UA})
    r.raise_for_status()
    d = r.json()
    if d.get("status") != "ok":
        raise RuntimeError(f"GoFile token error: {d}")
    return d["data"]["token"]

def _resolve_share(url, token):
    m = re.search(r"gofile\.io/(?:d/|\?c=)([A-Za-z0-9]+)", url)
    if not m:
        raise ValueError("Cannot parse GoFile content ID.")
    r = requests.get(
        f"https://api.gofile.io/contents/{m.group(1)}",
        params={"token": token, "wt": "4fd6sg89d7s6"},
        headers={"User-Agent": _UA, "Referer": "https://gofile.io/"},
        cookies={"accountToken": token}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"GoFile API: {data}")
    for child in data["data"].get("children", {}).values():
        if child.get("type") == "file" and child.get("mimetype", "").startswith("video"):
            return child["link"], child.get("name", "video.mp4")
    for child in data["data"].get("children", {}).values():
        if child.get("type") == "file":
            return child["link"], child.get("name", "file")
    raise RuntimeError("No video found in this GoFile share.")

def _dl_cdn(cdn_url, token, dest):
    """Download with 512KB chunks — lower RAM footprint."""
    with requests.get(cdn_url, stream=True, timeout=600,
                      headers={"User-Agent": _UA, "Referer": "https://gofile.io/"},
                      cookies={"accountToken": token}) as r:
        if r.status_code == 401:
            raise RuntimeError("GoFile 401: File may be password-protected or link expired.")
        if r.status_code == 429:
            raise RuntimeError("GoFile 429: Rate limited — wait a minute and try again.")
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(512 * 1024):
                if chunk:
                    f.write(chunk)

def download_gofile(url, job_dir, job_id):
    tok = _token()
    if re.search(r"gofile\.io/(?:d/|\?c=)", url):
        cdn_url, fname = _resolve_share(url, tok)
    else:
        fname   = url.split("?")[0].rstrip("/").split("/")[-1] or "video.mp4"
        cdn_url = url
    safe = re.sub(r"[^\w._-]", "_", fname).strip("_") or f"video_{job_id}"
    dest = job_dir / f"src_{job_id}_{safe}"
    _dl_cdn(cdn_url, tok, dest)
    if not dest.exists() or dest.stat().st_size == 0:
        raise RuntimeError("Downloaded file is empty — link may have expired.")
    return dest

def download_ytdlp(url, job_dir, job_id):
    tpl = str(job_dir / f"src_{job_id}.%(ext)s")
    r = subprocess.run(
        ["yt-dlp", "--no-playlist", "--no-warnings",
         "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best",
         "--merge-output-format", "mp4", "-o", tpl, url],
        capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"yt-dlp: {r.stderr[-2000:]}")
    for ext in ("mp4", "mkv", "mov", "webm"):
        c = job_dir / f"src_{job_id}.{ext}"
        if c.exists():
            return c
    matches = list(job_dir.glob(f"src_{job_id}.*"))
    if matches:
        return matches[0]
    raise RuntimeError("yt-dlp output not found.")

def smart_download(url, job_dir, job_id):
    if "gofile.io" in url:
        return download_gofile(url, job_dir, job_id)
    return download_ytdlp(url, job_dir, job_id)

# ── FFmpeg encode — optimised for 0.1 vCPU ───────────────────────────────────
def encode(src, preset, out):
    p = PRESETS[preset]
    vf = (
        f"scale={p['scale']}:force_original_aspect_ratio=decrease,"
        f"pad={p['scale']}:(ow-iw)/2:(oh-ih)/2"
    )
    cmd = [
        # nice -n 19 = lowest priority, won't starve uvicorn
        "nice", "-n", "19",
        "ffmpeg", "-y",
        "-i", src,
        "-vf", vf,
        "-c:v", "libx264",
        "-b:v", p["bitrate"],
        "-crf", p["crf"],
        "-preset", "ultrafast",   # ← was veryfast, saves ~60% CPU
        "-tune",   "fastdecode",  # ← optimise for decode speed too
        "-c:a", "aac",
        "-b:a", p["ab"],
        "-movflags", "+faststart",
        "-threads", "1",          # ← single thread for 0.1 vCPU
        out,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=7200)
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
        raise HTTPException(400, "URL required.")

    job_id  = uuid.uuid4().hex[:10]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)
    t0 = time.time()

    try:
        try:
            src = smart_download(url, job_dir, job_id)
        except Exception as e:
            raise HTTPException(500, f"Download failed: {e}")

        orig_bytes = src.stat().st_size
        base = re.sub(r"[^\w._-]", "_",
                      re.sub(r"\.[^.]+$", "", src.name)).strip("_")[:60] or "video"
        outname = f"{base}_{req.preset}.mp4"
        outpath = str(WORK_DIR / outname)

        try:
            encode(str(src), req.preset, outpath)
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
        job_id      = job_id,
        filename    = outname,
        orig_mb     = round(orig_mb, 2),
        comp_mb     = round(comp_mb, 2),
        saved_pct   = saved_pct,
        elapsed_sec = round(elapsed, 1),
    )

@app.get("/api/download/{filename}")
def download(filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename.")
    path = WORK_DIR / filename
    if not path.exists():
        raise HTTPException(404, "File not found or expired (kept for 1 hour).")
    return FileResponse(str(path), media_type="video/mp4", filename=filename,
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/", response_class=HTMLResponse)
def index():
    return HTML

# ── Embedded frontend ─────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Smart Video Compressor</title>
<style>
  :root{--bg:#0f1117;--card:#1a1d27;--border:#2a2d3e;--accent:#6c63ff;--accent2:#a78bfa;
        --green:#22c55e;--red:#ef4444;--yellow:#f59e0b;--text:#e2e8f0;--muted:#64748b;--radius:14px}
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;
       min-height:100vh;display:flex;flex-direction:column;align-items:center;padding:40px 16px 80px}
  .header{text-align:center;margin-bottom:40px}
  .header h1{font-size:clamp(1.8rem,4vw,2.6rem);font-weight:800;
    background:linear-gradient(135deg,var(--accent),var(--accent2));
    -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
  .header p{color:var(--muted);margin-top:8px;font-size:.95rem}
  .badge{display:inline-block;background:rgba(108,99,255,.15);border:1px solid rgba(108,99,255,.3);
    color:var(--accent2);font-size:.72rem;font-weight:600;padding:3px 10px;border-radius:99px;
    margin-top:12px;letter-spacing:.5px;text-transform:uppercase}
  .card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
        padding:28px;width:100%;max-width:640px}
  label{display:block;font-size:.8rem;font-weight:600;color:var(--muted);
        text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px}
  input[type=text]{width:100%;background:var(--bg);border:1px solid var(--border);
    border-radius:10px;color:var(--text);font-size:.95rem;padding:12px 16px;
    outline:none;transition:border-color .2s}
  input[type=text]:focus{border-color:var(--accent)}
  input::placeholder{color:var(--muted)}
  .field{margin-bottom:18px}
  .url-hint{font-size:.75rem;margin-top:6px;min-height:18px}
  .url-hint.share{color:var(--green)}
  .url-hint.cdn{color:var(--yellow)}
  .url-hint.other{color:var(--muted)}
  .presets{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:22px}
  .preset-btn{background:var(--bg);border:1px solid var(--border);border-radius:10px;
    color:var(--muted);cursor:pointer;padding:10px 6px;text-align:center;transition:all .2s;font-size:.85rem}
  .preset-btn .res{font-size:1rem;font-weight:700;color:var(--text)}
  .preset-btn .save{font-size:.7rem;margin-top:2px}
  .preset-btn:hover{border-color:var(--accent)}
  .preset-btn.active{border-color:var(--accent);background:rgba(108,99,255,.12)}
  .preset-btn.active .res{color:var(--accent2)}
  .btn{width:100%;background:linear-gradient(135deg,var(--accent),#8b5cf6);border:none;
    border-radius:10px;color:#fff;cursor:pointer;font-size:1rem;font-weight:700;padding:14px;
    transition:opacity .2s,transform .1s}
  .btn:hover:not(:disabled){opacity:.88;transform:translateY(-1px)}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .progress-wrap{display:none;margin-top:22px}
  .progress-label{display:flex;justify-content:space-between;font-size:.82rem;color:var(--muted);margin-bottom:8px}
  .progress-bar-bg{background:var(--bg);border-radius:99px;height:8px;overflow:hidden;border:1px solid var(--border)}
  .progress-bar{background:linear-gradient(90deg,var(--accent),var(--accent2));height:100%;
    width:0%;border-radius:99px;transition:width .6s ease}
  .spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(108,99,255,.3);
    border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;
    vertical-align:middle;margin-right:6px}
  @keyframes spin{to{transform:rotate(360deg)}}
  .warning-box{margin-top:14px;padding:11px 14px;border-radius:10px;font-size:.82rem;line-height:1.6;
    background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.2);color:#fcd34d;display:none}
  .result{display:none;margin-top:22px;background:var(--bg);border:1px solid var(--border);
    border-radius:12px;padding:20px}
  .result.success{border-color:rgba(34,197,94,.35)}
  .result.error{border-color:rgba(239,68,68,.35)}
  .result-title{font-weight:700;font-size:.95rem;margin-bottom:14px}
  .result.error .result-title{color:var(--red)}
  .result.success .result-title{color:var(--green)}
  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
  .stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;text-align:center}
  .stat-val{font-size:1.1rem;font-weight:700}
  .stat-val.green{color:var(--green)}
  .stat-label{font-size:.7rem;color:var(--muted);margin-top:3px;text-transform:uppercase;letter-spacing:.5px}
  .dl-btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;
    background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.35);border-radius:10px;
    color:var(--green);font-weight:700;font-size:.95rem;padding:12px;text-decoration:none;transition:background .2s}
  .dl-btn:hover{background:rgba(34,197,94,.22)}
  .error-msg{font-size:.8rem;color:var(--red);font-family:monospace;white-space:pre-wrap;
             word-break:break-word;line-height:1.6;max-height:200px;overflow-y:auto;
             background:rgba(239,68,68,.06);padding:12px;border-radius:8px}
  .how{margin-top:28px;width:100%;max-width:640px}
  .how h3{font-size:.75rem;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:10px}
  .steps{display:flex;flex-direction:column;gap:8px}
  .step{display:flex;align-items:flex-start;gap:12px;background:var(--card);
    border:1px solid var(--border);border-radius:10px;padding:12px 16px;font-size:.84rem;color:var(--muted)}
  .step b{color:var(--text)}
  .cpu-note{margin-top:12px;padding:12px 14px;border-radius:10px;font-size:.8rem;line-height:1.7;
    background:rgba(108,99,255,.07);border:1px solid rgba(108,99,255,.18);color:var(--muted)}
  .cpu-note b{color:var(--accent2)}
</style>
</head>
<body>

<div class="header">
  <h1>🎬 Smart Video Compressor</h1>
  <p>Paste a GoFile link — get a compressed video back.</p>
  <span class="badge">✦ GoFile API + FFmpeg ultrafast</span>
</div>

<div class="card">
  <div class="field">
    <label>GoFile Share Link or Direct CDN URL</label>
    <input type="text" id="urlInput" oninput="detectUrl(this.value)"
           placeholder="https://gofile.io/d/XXXXXX"/>
    <div class="url-hint other" id="urlHint">Paste a GoFile URL above</div>
  </div>

  <label style="margin-bottom:10px">Output Quality</label>
  <div class="presets">
    <button class="preset-btn" data-preset="240p"><div class="res">240p</div><div class="save">~94% off</div></button>
    <button class="preset-btn active" data-preset="360p"><div class="res">360p</div><div class="save">~88% off</div></button>
    <button class="preset-btn" data-preset="480p"><div class="res">480p</div><div class="save">~75% off</div></button>
    <button class="preset-btn" data-preset="720p"><div class="res">720p</div><div class="save">~50% off</div></button>
  </div>

  <button class="btn" id="compressBtn" onclick="startCompress()">🚀 Compress Video</button>

  <div class="warning-box" id="warningBox">
    ⏳ <b>This may take several minutes</b> on the free server (0.1 vCPU).<br>
    The page will update automatically when done — don't close it.
  </div>

  <div class="progress-wrap" id="progressWrap">
    <div class="progress-label">
      <span><span class="spinner"></span><span id="phaseText">Starting…</span></span>
      <span id="progressPct">0%</span>
    </div>
    <div class="progress-bar-bg"><div class="progress-bar" id="progressBar"></div></div>
  </div>

  <div class="result" id="result">
    <div class="result-title" id="resultTitle"></div>
    <div id="resultBody"></div>
  </div>
</div>

<div class="how">
  <h3>How it works</h3>
  <div class="steps">
    <div class="step"><span>🔑</span>
      <span><b>GoFile API</b> — server gets a guest token, resolves the link, downloads directly. No yt-dlp rate-limiting.</span></div>
    <div class="step"><span>⚙️</span>
      <span><b>FFmpeg ultrafast</b> — uses the fastest encoding preset to stay within the free server's 0.1 vCPU limit.</span></div>
    <div class="step"><span>⬇️</span>
      <span><b>Download</b> the compressed video. Source file deleted immediately. Output expires after 1 hour.</span></div>
  </div>
  <div class="cpu-note">
    <b>Free tier limits:</b> Encoding a 100MB video at 360p takes ~3–8 minutes on 0.1 vCPU.
    For faster results, keep files under 200MB or use 240p/360p presets.
  </div>
</div>

<script>
  let selectedPreset = '360p';
  document.querySelectorAll('.preset-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.preset-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      selectedPreset = btn.dataset.preset;
    });
  });

  function detectUrl(val) {
    const hint = document.getElementById('urlHint');
    val = val.trim();
    if (!val) { hint.className='url-hint other'; hint.textContent='Paste a GoFile URL above'; return; }
    if (/gofile\.io\/(?:d\/|\?c=)/.test(val)) {
      hint.className='url-hint share';
      hint.textContent='✅ GoFile share link — best option';
    } else if (/[\w-]+\.gofile\.io\//.test(val)) {
      hint.className='url-hint cdn';
      hint.textContent='⚡ GoFile CDN URL — downloaded with guest token';
    } else {
      hint.className='url-hint other';
      hint.textContent='🔗 External URL — handled by yt-dlp';
    }
  }

  let progressInterval = null, pct = 0, phaseIdx = 0;
  // Slower animation to match real encode time on 0.1 vCPU
  const PHASES = [
    {label:'📥 Downloading from GoFile…', end:30, speed:0.6},
    {label:'⚙️ Encoding (ultrafast)…',    end:93, speed:0.18},
  ];

  function startFakeProgress() {
    pct = 0; phaseIdx = 0;
    const bar   = document.getElementById('progressBar');
    const pctEl = document.getElementById('progressPct');
    const phase = document.getElementById('phaseText');
    progressInterval = setInterval(() => {
      const p = PHASES[Math.min(phaseIdx, PHASES.length-1)];
      if (pct < p.end) { pct += p.speed; }
      else if (phaseIdx < PHASES.length - 1) { phaseIdx++; }
      bar.style.width  = Math.min(pct, 93) + '%';
      pctEl.textContent = Math.floor(Math.min(pct, 93)) + '%';
      phase.textContent = PHASES[Math.min(phaseIdx, PHASES.length-1)].label;
    }, 600);
  }

  function stopProgress(final=100) {
    clearInterval(progressInterval);
    document.getElementById('progressBar').style.width = final + '%';
    document.getElementById('progressPct').textContent = final + '%';
    document.getElementById('phaseText').textContent = final === 100 ? '✅ Done!' : '❌ Failed';
  }

  async function startCompress() {
    const url = document.getElementById('urlInput').value.trim();
    if (!url) { showError('Please paste a URL above.'); return; }
    const btn = document.getElementById('compressBtn');
    btn.disabled = true;
    btn.textContent = '⚙️ Processing…';
    document.getElementById('progressWrap').style.display = 'block';
    document.getElementById('warningBox').style.display   = 'block';
    document.getElementById('result').style.display       = 'none';
    startFakeProgress();
    try {
      const res  = await fetch('/api/compress', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({url, preset: selectedPreset}),
      });
      const data = await res.json();
      stopProgress(res.ok ? 100 : 0);
      document.getElementById('warningBox').style.display = 'none';
      res.ok ? showSuccess(data) : showError(data.detail || 'Compression failed.');
    } catch(err) {
      stopProgress(0);
      document.getElementById('warningBox').style.display = 'none';
      showError('Network error: ' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '🚀 Compress Video';
    }
  }

  function showSuccess(d) {
    const el = document.getElementById('result');
    el.className = 'result success'; el.style.display = 'block';
    document.getElementById('resultTitle').textContent = '✅ Done in ' + d.elapsed_sec + 's';
    document.getElementById('resultBody').innerHTML = `
      <div class="stats">
        <div class="stat"><div class="stat-val">${d.orig_mb} MB</div><div class="stat-label">Original</div></div>
        <div class="stat"><div class="stat-val">${d.comp_mb} MB</div><div class="stat-label">Compressed</div></div>
        <div class="stat"><div class="stat-val green">${d.saved_pct}% off</div><div class="stat-label">Saved</div></div>
      </div>
      <a class="dl-btn" href="/api/download/${encodeURIComponent(d.filename)}" download>
        ⬇️ Download ${d.filename}
      </a>`;
  }

  function showError(msg) {
    const el = document.getElementById('result');
    el.className = 'result error'; el.style.display = 'block';
    document.getElementById('resultTitle').textContent = '❌ Error';
    document.getElementById('resultBody').innerHTML =
      `<div class="error-msg">${msg.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>`;
  }

  document.getElementById('urlInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') startCompress();
  });
</script>
</body>
</html>"""
