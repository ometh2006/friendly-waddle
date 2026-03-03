"""
Smart Video Compressor — FastAPI backend
HTML is embedded directly — no static/ folder needed.
"""
import os, re, uuid, time, subprocess, threading, requests
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

app = FastAPI(title="Smart Video Compressor")

WORK_DIR = Path("/tmp/compressed")
WORK_DIR.mkdir(exist_ok=True)

# ── auto-cleanup files older than 1 hour ─────────────────────────────────────
def _cleanup():
    while True:
        time.sleep(1800)
        now = time.time()
        for f in WORK_DIR.glob("*.mp4"):
            if now - f.stat().st_mtime > 3600:
                f.unlink(missing_ok=True)

threading.Thread(target=_cleanup, daemon=True).start()

# ── Presets ───────────────────────────────────────────────────────────────────
PRESETS = {
    "240p": {"scale": "426:240",  "bitrate": "180k",  "ab": "48k",  "crf": "32"},
    "360p": {"scale": "640:360",  "bitrate": "350k",  "ab": "64k",  "crf": "30"},
    "480p": {"scale": "854:480",  "bitrate": "700k",  "ab": "96k",  "crf": "28"},
    "720p": {"scale": "1280:720", "bitrate": "1500k", "ab": "128k", "crf": "24"},
}

_UA      = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
_REFERER = "https://gofile.io/"
_sess    = requests.Session()
_sess.headers.update({"User-Agent": _UA, "Referer": _REFERER})

# ── GoFile API ────────────────────────────────────────────────────────────────
def _gofile_token():
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

# ── FFmpeg encoder ────────────────────────────────────────────────────────────
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
    try:
        direct_url, fname, orig_bytes, token = resolve_url(url)
    except Exception as e:
        raise HTTPException(400, f"URL resolve error: {e}")

    job_id  = uuid.uuid4().hex[:10]
    base    = re.sub(r"\.[^.]+$", "", fname)
    base    = re.sub(r"[^\w._-]", "_", base).strip("_") or "video"
    outname = f"{base}_{req.preset}_{job_id}.mp4"
    outpath = str(WORK_DIR / outname)

    t0 = time.time()
    try:
        encode(direct_url, req.preset, outpath, token=token)
    except Exception as e:
        raise HTTPException(500, f"Encoding failed: {e}")

    elapsed    = time.time() - t0
    comp_bytes = os.path.getsize(outpath)
    orig_mb    = orig_bytes / (1024*1024) if orig_bytes else comp_bytes / (1024*1024)
    comp_mb    = comp_bytes / (1024*1024)
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
        raise HTTPException(404, "File not found or expired.")
    return FileResponse(
        path       = str(path),
        media_type = "video/mp4",
        filename   = filename,
        headers    = {"Content-Disposition": f'attachment; filename="{filename}"'},
    )

@app.get("/health")
def health():
    return {"status": "ok"}

# ── Frontend (embedded — no static/ folder needed) ───────────────────────────
@app.get("/", response_class=HTMLResponse)
def index():
    return HTML

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>Smart Video Compressor</title>
<style>
  :root {
    --bg:#0f1117;--card:#1a1d27;--border:#2a2d3e;
    --accent:#6c63ff;--accent2:#a78bfa;
    --green:#22c55e;--red:#ef4444;
    --text:#e2e8f0;--muted:#64748b;--radius:14px;
  }
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
        padding:28px;width:100%;max-width:620px}
  label{display:block;font-size:.8rem;font-weight:600;color:var(--muted);
        text-transform:uppercase;letter-spacing:.6px;margin-bottom:8px}
  input[type=text]{width:100%;background:var(--bg);border:1px solid var(--border);
    border-radius:10px;color:var(--text);font-size:.95rem;padding:12px 16px;
    outline:none;transition:border-color .2s}
  input[type=text]:focus{border-color:var(--accent)}
  input::placeholder{color:var(--muted)}
  .field{margin-bottom:18px}
  .presets{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:22px}
  .preset-btn{background:var(--bg);border:1px solid var(--border);border-radius:10px;
    color:var(--muted);cursor:pointer;padding:10px 6px;text-align:center;transition:all .2s;font-size:.85rem}
  .preset-btn .res{font-size:1rem;font-weight:700;color:var(--text)}
  .preset-btn .save{font-size:.7rem;margin-top:2px}
  .preset-btn:hover{border-color:var(--accent)}
  .preset-btn.active{border-color:var(--accent);background:rgba(108,99,255,.12);color:var(--accent2)}
  .preset-btn.active .res{color:var(--accent2)}
  .btn{width:100%;background:linear-gradient(135deg,var(--accent),#8b5cf6);border:none;
    border-radius:10px;color:#fff;cursor:pointer;font-size:1rem;font-weight:700;padding:14px;
    transition:opacity .2s,transform .1s;letter-spacing:.3px}
  .btn:hover:not(:disabled){opacity:.88;transform:translateY(-1px)}
  .btn:disabled{opacity:.45;cursor:not-allowed}
  .progress-wrap{display:none;margin-top:22px}
  .progress-label{display:flex;justify-content:space-between;font-size:.82rem;color:var(--muted);margin-bottom:8px}
  .progress-bar-bg{background:var(--bg);border-radius:99px;height:8px;overflow:hidden;border:1px solid var(--border)}
  .progress-bar{background:linear-gradient(90deg,var(--accent),var(--accent2));height:100%;
    width:0%;border-radius:99px;transition:width .4s ease}
  .spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(108,99,255,.3);
    border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;
    vertical-align:middle;margin-right:6px}
  @keyframes spin{to{transform:rotate(360deg)}}
  .result{display:none;margin-top:22px;background:var(--bg);border:1px solid var(--border);
    border-radius:12px;padding:20px}
  .result.success{border-color:rgba(34,197,94,.35)}
  .result.error{border-color:rgba(239,68,68,.35)}
  .result-title{font-weight:700;font-size:.95rem;margin-bottom:14px;display:flex;align-items:center;gap:8px}
  .result.error .result-title{color:var(--red)}
  .result.success .result-title{color:var(--green)}
  .stats{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px}
  .stat{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px;text-align:center}
  .stat-val{font-size:1.1rem;font-weight:700;color:var(--text)}
  .stat-val.green{color:var(--green)}
  .stat-label{font-size:.7rem;color:var(--muted);margin-top:3px;text-transform:uppercase;letter-spacing:.5px}
  .dl-btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;
    background:rgba(34,197,94,.12);border:1px solid rgba(34,197,94,.35);border-radius:10px;
    color:var(--green);font-weight:700;font-size:.95rem;padding:12px;cursor:pointer;
    text-decoration:none;transition:background .2s}
  .dl-btn:hover{background:rgba(34,197,94,.22)}
  .error-msg{font-size:.82rem;color:var(--red);font-family:monospace;white-space:pre-wrap;word-break:break-word;line-height:1.5}
  .how{margin-top:32px;width:100%;max-width:620px}
  .how h3{font-size:.75rem;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:12px}
  .steps{display:flex;flex-direction:column;gap:8px}
  .step{display:flex;align-items:flex-start;gap:12px;background:var(--card);
    border:1px solid var(--border);border-radius:10px;padding:12px 16px;font-size:.85rem;color:var(--muted)}
  .step-icon{font-size:1.1rem;flex-shrink:0}
  .step b{color:var(--text)}
</style>
</head>
<body>

<div class="header">
  <h1>🎬 Smart Video Compressor</h1>
  <p>Paste a GoFile link — get back a small, compressed video file.</p>
  <span class="badge">✦ HD file never saved to disk</span>
</div>

<div class="card">
  <div class="field">
    <label>GoFile Share Link or Direct Video URL</label>
    <input type="text" id="urlInput"
           placeholder="https://gofile.io/d/XXXXXX  or  https://…/video.mp4"/>
  </div>

  <label style="margin-bottom:10px">Output Quality</label>
  <div class="presets">
    <button class="preset-btn" data-preset="240p">
      <div class="res">240p</div><div class="save">~94% off</div>
    </button>
    <button class="preset-btn active" data-preset="360p">
      <div class="res">360p</div><div class="save">~88% off</div>
    </button>
    <button class="preset-btn" data-preset="480p">
      <div class="res">480p</div><div class="save">~75% off</div>
    </button>
    <button class="preset-btn" data-preset="720p">
      <div class="res">720p</div><div class="save">~50% off</div>
    </button>
  </div>

  <button class="btn" id="compressBtn" onclick="startCompress()">🚀 Compress Video</button>

  <div class="progress-wrap" id="progressWrap">
    <div class="progress-label">
      <span id="progressText"><span class="spinner"></span>Processing…</span>
      <span id="progressPct"></span>
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
    <div class="step">
      <span class="step-icon">🔎</span>
      <span><b>GoFile API</b> — paste your share link and the server auto-extracts the direct video URL.</span>
    </div>
    <div class="step">
      <span class="step-icon">⚡</span>
      <span><b>Stream → FFmpeg</b> — video is fetched over HTTP and encoded on-the-fly. <b>HD file never stored.</b></span>
    </div>
    <div class="step">
      <span class="step-icon">⬇️</span>
      <span><b>Instant download</b> — click the download button once encoding finishes.</span>
    </div>
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

  let progressInterval = null;
  function startFakeProgress() {
    let pct = 0;
    const bar = document.getElementById('progressBar');
    const pctEl = document.getElementById('progressPct');
    progressInterval = setInterval(() => {
      if (pct < 30)      pct += 2.5;
      else if (pct < 70) pct += 0.8;
      else if (pct < 88) pct += 0.2;
      bar.style.width = pct + '%';
      pctEl.textContent = Math.floor(pct) + '%';
    }, 400);
  }
  function stopProgress(final=100) {
    clearInterval(progressInterval);
    document.getElementById('progressBar').style.width = final + '%';
    document.getElementById('progressPct').textContent = final + '%';
  }

  async function startCompress() {
    const url = document.getElementById('urlInput').value.trim();
    if (!url) { showError('Please paste a URL above.'); return; }
    const btn = document.getElementById('compressBtn');
    btn.disabled = true;
    btn.textContent = '⚙️ Compressing…';
    document.getElementById('progressWrap').style.display = 'block';
    document.getElementById('result').style.display = 'none';
    document.getElementById('progressText').innerHTML = '<span class="spinner"></span>Encoding video… (may take a few minutes)';
    startFakeProgress();
    try {
      const res  = await fetch('/api/compress', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({url, preset: selectedPreset}),
      });
      const data = await res.json();
      stopProgress(100);
      res.ok ? showSuccess(data) : showError(data.detail || 'Compression failed.');
    } catch(err) {
      stopProgress(0);
      showError('Network error: ' + err.message);
    } finally {
      btn.disabled = false;
      btn.textContent = '🚀 Compress Video';
    }
  }

  function showSuccess(d) {
    const el = document.getElementById('result');
    el.className = 'result success';
    el.style.display = 'block';
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
    el.className = 'result error';
    el.style.display = 'block';
    document.getElementById('resultTitle').textContent = '❌ Error';
    document.getElementById('resultBody').innerHTML = `<div class="error-msg">${msg.replace(/</g,'&lt;')}</div>`;
  }

  document.getElementById('urlInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') startCompress();
  });
</script>
</body>
</html>"""
