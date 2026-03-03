"""
Smart Video Compressor — FastAPI + yt-dlp + FFmpeg
yt-dlp handles GoFile auth/redirects/MOV files reliably.
Flow: GoFile URL → yt-dlp download to /tmp → FFmpeg encode → serve download
"""
import os, re, uuid, time, subprocess, threading, shutil, requests
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
        for f in WORK_DIR.glob("*"):
            try:
                if now - f.stat().st_mtime > 3600:
                    f.unlink(missing_ok=True)
            except Exception:
                pass

threading.Thread(target=_cleanup, daemon=True).start()

# ── Presets ───────────────────────────────────────────────────────────────────
PRESETS = {
    "240p": {"scale": "426:240",  "bitrate": "180k",  "ab": "48k",  "crf": "32"},
    "360p": {"scale": "640:360",  "bitrate": "350k",  "ab": "64k",  "crf": "30"},
    "480p": {"scale": "854:480",  "bitrate": "700k",  "ab": "96k",  "crf": "28"},
    "720p": {"scale": "1280:720", "bitrate": "1500k", "ab": "128k", "crf": "24"},
}

# ── Download with yt-dlp (handles GoFile auth + MOV/MP4) ─────────────────────
def download_with_ytdlp(url: str, out_dir: Path, job_id: str) -> Path:
    """
    Use yt-dlp to download the video to out_dir.
    yt-dlp has a native GoFile extractor — handles cookies, redirects, MOV files.
    Returns the path of the downloaded file.
    """
    out_template = str(out_dir / f"src_{job_id}.%(ext)s")

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "-o", out_template,
        url,
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode != 0:
        raise RuntimeError(
            f"yt-dlp failed (code {result.returncode}):\n"
            f"{result.stderr[-2000:]}"
        )

    # Find the downloaded file
    for ext in ("mp4", "mkv", "mov", "webm", "avi"):
        candidate = out_dir / f"src_{job_id}.{ext}"
        if candidate.exists():
            return candidate

    # Fallback: find any src_ file
    matches = list(out_dir.glob(f"src_{job_id}.*"))
    if matches:
        return matches[0]

    raise RuntimeError("yt-dlp ran but output file not found.")

# ── FFmpeg encode from local file ─────────────────────────────────────────────
def encode(src_path: str, preset: str, output_path: str):
    """
    Encode a local file to the target preset.
    Source file is local so moov/seek issues don't apply.
    """
    p  = PRESETS[preset]
    vf = (
        f"scale={p['scale']}:force_original_aspect_ratio=decrease,"
        f"pad={p['scale']}:(ow-iw)/2:(oh-ih)/2"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", src_path,
        "-vf", vf,
        "-c:v", "libx264", "-b:v", p["bitrate"], "-crf", p["crf"],
        "-c:a", "aac",     "-b:a", p["ab"],
        "-preset", "veryfast",
        "-movflags", "+faststart",
        "-threads", "2",
        output_path,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
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

    job_id  = uuid.uuid4().hex[:10]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    src_path = None
    t0 = time.time()

    try:
        # Step 1: download via yt-dlp
        try:
            src_path = download_with_ytdlp(url, job_dir, job_id)
        except Exception as e:
            raise HTTPException(500, f"Download failed: {e}")

        orig_bytes = src_path.stat().st_size
        orig_name  = src_path.stem  # filename without extension

        # Step 2: encode with FFmpeg
        outname = f"{orig_name}_{req.preset}.mp4"
        outpath = str(WORK_DIR / outname)

        try:
            encode(str(src_path), req.preset, outpath)
        except Exception as e:
            raise HTTPException(500, f"Encoding failed: {e}")

    finally:
        # Always clean up the source download
        if job_dir.exists():
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
        raise HTTPException(404, "File not found or expired (files are kept for 1 hour).")
    return FileResponse(
        path       = str(path),
        media_type = "video/mp4",
        filename   = filename,
        headers    = {"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


# ── Embedded Frontend ─────────────────────────────────────────────────────────
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
        padding:28px;width:100%;max-width:640px}
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
    width:0%;border-radius:99px;transition:width .5s ease}
  .spinner{display:inline-block;width:14px;height:14px;border:2px solid rgba(108,99,255,.3);
    border-top-color:var(--accent);border-radius:50%;animation:spin .7s linear infinite;
    vertical-align:middle;margin-right:6px}
  @keyframes spin{to{transform:rotate(360deg)}}
  .phase{font-size:.75rem;color:var(--accent2);font-weight:600;letter-spacing:.4px;
         text-transform:uppercase;margin-top:6px}
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
  .error-msg{font-size:.8rem;color:var(--red);font-family:monospace;white-space:pre-wrap;
             word-break:break-word;line-height:1.5;max-height:220px;overflow-y:auto;
             background:rgba(239,68,68,.06);padding:12px;border-radius:8px}
  .how{margin-top:32px;width:100%;max-width:640px}
  .how h3{font-size:.75rem;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);margin-bottom:12px}
  .steps{display:flex;flex-direction:column;gap:8px}
  .step{display:flex;align-items:flex-start;gap:12px;background:var(--card);
    border:1px solid var(--border);border-radius:10px;padding:12px 16px;font-size:.85rem;color:var(--muted)}
  .step-icon{font-size:1.1rem;flex-shrink:0}
  .step b{color:var(--text)}
  .tip{margin-top:16px;padding:12px 16px;background:rgba(108,99,255,.08);
    border:1px solid rgba(108,99,255,.2);border-radius:10px;font-size:.82rem;color:var(--muted)}
  .tip b{color:var(--accent2)}
</style>
</head>
<body>

<div class="header">
  <h1>🎬 Smart Video Compressor</h1>
  <p>Paste a GoFile link — get a compressed video back.</p>
  <span class="badge">✦ Powered by yt-dlp + FFmpeg</span>
</div>

<div class="card">
  <div class="field">
    <label>GoFile Share Link or Direct Video URL</label>
    <input type="text" id="urlInput"
           placeholder="https://gofile.io/d/XXXXXX"/>
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
      <span id="progressText"><span class="spinner"></span><span id="phaseText">Starting…</span></span>
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
    <div class="step">
      <span class="step-icon">📥</span>
      <span><b>yt-dlp</b> downloads the video from GoFile — handles authentication, redirects, MOV &amp; MP4 files automatically.</span>
    </div>
    <div class="step">
      <span class="step-icon">⚙️</span>
      <span><b>FFmpeg</b> re-encodes the local file to your chosen quality. No moov-atom or seek errors.</span>
    </div>
    <div class="step">
      <span class="step-icon">⬇️</span>
      <span><b>Download</b> the compressed file. Original source is deleted immediately after encoding.</span>
    </div>
  </div>
  <div class="tip">
    <b>Supported inputs:</b> GoFile share links (<code>gofile.io/d/…</code>),
    direct .mp4 / .mov / .mkv URLs, and most video hosting sites supported by yt-dlp.
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
  let currentPhase = 0; // 0=download, 1=encode

  const PHASES = [
    { label: '📥 Downloading…',  start: 0,   end: 45,  speed: 1.2 },
    { label: '⚙️ Encoding…',     start: 45,  end: 95,  speed: 0.5 },
  ];

  function startFakeProgress() {
    let pct = 0;
    currentPhase = 0;
    const bar    = document.getElementById('progressBar');
    const pctEl  = document.getElementById('progressPct');
    const phase  = document.getElementById('phaseText');

    progressInterval = setInterval(() => {
      const p = currentPhase < PHASES.length ? PHASES[currentPhase] : PHASES[PHASES.length-1];
      if (pct < p.end) {
        pct += p.speed;
      } else if (currentPhase < PHASES.length - 1) {
        currentPhase++;
      }
      bar.style.width = Math.min(pct, 98) + '%';
      pctEl.textContent = Math.floor(Math.min(pct, 98)) + '%';
      phase.textContent = PHASES[Math.min(currentPhase, PHASES.length-1)].label;
    }, 500);
  }

  // Advance to encoding phase (called mid-request if we could detect it — here just time-based)
  function stopProgress(final=100) {
    clearInterval(progressInterval);
    document.getElementById('progressBar').style.width = final + '%';
    document.getElementById('progressPct').textContent = final + '%';
    document.getElementById('phaseText').textContent = final === 100 ? '✅ Done!' : 'Failed';
  }

  async function startCompress() {
    const url = document.getElementById('urlInput').value.trim();
    if (!url) { showError('Please paste a GoFile link or video URL above.'); return; }

    const btn = document.getElementById('compressBtn');
    btn.disabled = true;
    btn.textContent = '⚙️ Processing…';
    document.getElementById('progressWrap').style.display = 'block';
    document.getElementById('result').style.display = 'none';
    startFakeProgress();

    try {
      const res  = await fetch('/api/compress', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({url, preset: selectedPreset}),
      });
      const data = await res.json();
      stopProgress(res.ok ? 100 : 0);
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
    document.getElementById('resultBody').innerHTML =
      `<div class="error-msg">${msg.replace(/</g,'&lt;').replace(/>/g,'&gt;')}</div>`;
  }

  document.getElementById('urlInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') startCompress();
  });
</script>
</body>
</html>"""
