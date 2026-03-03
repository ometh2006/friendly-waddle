# 🎬 Smart Video Compressor

A self-hosted web tool that compresses GoFile videos without saving the HD file to disk.
Built with **FastAPI + FFmpeg**, deployable to **Koyeb free tier** in under 5 minutes.

---

## Project structure

```
smart-compressor/
├── app.py              ← FastAPI backend
├── static/
│   └── index.html      ← Web UI (served by FastAPI)
├── Dockerfile          ← Container build
├── requirements.txt    ← Python deps
└── README.md
```

---

## Deploy to Koyeb (free, step by step)

### 1 — Push to GitHub

Create a new GitHub repo and push this entire folder:

```bash
git init
git add .
git commit -m "initial"
git remote add origin https://github.com/YOUR_USERNAME/smart-compressor.git
git push -u origin main
```

### 2 — Create a free Koyeb account

Go to https://app.koyeb.com → sign up (no credit card needed for free tier).

### 3 — Create a new Service on Koyeb

1. Click **"Create Service"**
2. Choose **"GitHub"** as the source
3. Select your repo and branch (`main`)
4. Under **Builder**, select **"Dockerfile"**
5. Under **"Exposed ports"**, set port **`8000`**
6. Click **Deploy**

Koyeb will:
- Build the Docker image (installs FFmpeg + Python deps)
- Deploy it on a free instance
- Give you a public URL like `https://your-app-xyz.koyeb.app`

### 4 — Open your URL

Visit `https://your-app-xyz.koyeb.app` — the web UI loads instantly.

---

## How to use the UI

1. Paste a **GoFile share link** (`https://gofile.io/d/XXXXXX`)
   — OR — a **direct `.mp4` URL**
2. Pick a quality preset: **240p / 360p / 480p / 720p**
3. Click **Compress Video**
4. Wait for encoding to finish (progress bar animates)
5. Click **Download** to save the compressed file

---

## How it works (technical)

```
Browser  →  POST /api/compress  →  FastAPI
                                      │
                              GoFile API → direct URL
                                      │
                              FFmpeg -i <url>   ← streams over HTTP
                              (HD file never written to disk)
                                      │
                              Writes compressed .mp4 to /tmp
                                      │
Browser  ←  GET /api/download/file.mp4  ← FileResponse
```

**Why no stdin pipe?**
MP4 files store their `moov` metadata atom at the END of the file.
FFmpeg needs to seek backwards to read it before decoding.
Stdin pipes don't support seeking → `"Invalid data found"` error.

**The fix:** Pass the URL directly as `-i <url>` with `-headers` for authentication.
FFmpeg's built-in HTTP client handles byte-range seeks internally.

---

## Koyeb free tier limits

| Resource | Free allowance |
|----------|---------------|
| Services | 2             |
| RAM      | 512 MB        |
| vCPU     | 0.1           |
| Bandwidth| 100 GB/month  |
| Storage  | Ephemeral /tmp|

> Compressed files are stored in `/tmp` and auto-deleted after 1 hour.
> The 512 MB RAM is enough for encoding at 360p/480p.
> For 720p on large files, encoding may be slower but will work.

---

## Local development

```bash
# Install deps
pip install -r requirements.txt

# Install FFmpeg (Mac)
brew install ffmpeg

# Install FFmpeg (Ubuntu/Debian)
sudo apt-get install ffmpeg

# Run locally
uvicorn app:app --reload --port 8000
# Open: http://localhost:8000
```

---

## Customising presets

Edit `PRESETS` in `app.py`:

```python
PRESETS = {
    "360p": {"scale": "640:360",  "bitrate": "350k", "ab": "64k",  "crf": "30"},
    "480p": {"scale": "854:480",  "bitrate": "700k", "ab": "96k",  "crf": "28"},
    ...
}
```

Lower `crf` = better quality but larger file (range: 18–35).
Higher `bitrate` = more detail but larger file.
