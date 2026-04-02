# 📥 VidSave — Multi-Platform Video Downloader

A fast, clean web app for downloading videos from your favourite social platforms. Paste a link, pick a format, and save — no accounts, no extensions required.

**🌐 Live at → [video-downloader-eta-drab.vercel.app](https://video-downloader-eta-drab.vercel.app/)**

---

## Supported Platforms

| Platform | Status |
|---|---|
| YouTube | ✅ |
| TikTok | ✅ |
| Instagram | ✅ |
| Facebook | ✅ |
| X (Twitter) | ✅ |

---

## Features

- Paste any supported URL and get video metadata instantly — title, thumbnail, duration, uploader
- Choose your preferred quality and format before downloading
- Schedule downloads for later
- Download history saved locally in your browser
- Rate limited and secured against private network abuse
- No database — stateless and serverless-friendly

---

## Stack

- **Backend** — Python, Flask, yt-dlp
- **Frontend** — Vanilla HTML/CSS/JS
- **Deployment** — Vercel

---

## Running Locally

```bash
# Clone the repo
git clone <your-repo-url>
cd <project-folder>

# Set up virtual environment
python -m venv venv

# Activate (Windows PowerShell)
.\venv\Scripts\Activate.ps1

# Activate (Mac/Linux)
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
pip install -U yt-dlp

# Start the server
python vid.py
```

Then open [http://127.0.0.1:5000](http://127.0.0.1:5000)

---

## API

### `GET /api/health`
Returns service status and available endpoints.

```json
{
  "status": "ok",
  "endpoints": ["/api/health", "/api/download", "/api/metadata", "/api/schedule", "/api/debug/ydlp-version", "/history", "/schedule"],
  "supported_platforms": ["facebook", "instagram", "tiktok", "twitter", "youtube"]
}
```

---

### `POST /api/metadata`
Fetches video info without downloading.

**Request**
```json
{ "url": "https://www.youtube.com/watch?v=abc123" }
```

**Response**
```json
{
  "title": "Example Video",
  "thumbnail": "https://example.com/thumb.jpg",
  "duration": 187,
  "uploader": "Example Channel",
  "platform": "youtube",
  "formats": [
    { "format_id": "22", "ext": "mp4", "height": 720, "filesize": 7000000, "label": "720p MP4" },
    { "format_id": "audio-only", "ext": "mp3", "height": null, "filesize": null, "label": "Audio only (MP3)" }
  ]
}
```

---

### `POST /api/download`
Downloads and streams the media file.

**Request**
```json
{
  "url": "https://www.youtube.com/watch?v=abc123",
  "download_type": "video",
  "quality": "best",
  "format_id": "22"
}
```

`download_type` — `video` or `audio`  
`quality` — `best`, `1080`, `720`, `480`, or `360`  
`format_id` — optional, alphanumeric + `-` or `+` only, max 20 chars

---

### `GET /api/debug/ydlp-version`
Returns the installed yt-dlp version — useful for debugging extractor issues.

```json
{
  "yt_dlp_version": "2025.02.19",
  "tiktok_status": "ok"
}
```

---

## Rate Limiting

Both `/api/download` and `/api/metadata` are limited to **5 requests per minute per IP**.

```json
{
  "error": "Rate limit exceeded",
  "retry_after": 60
}
```

---

## Configuration

All config is via environment variables — no `.env` file required for local dev, defaults are sensible.

| Variable | Default | Description |
|---|---|---|
| `PORT` | `5000` | Server port |
| `FLASK_DEBUG` | `0` | Debug mode |
| `LOG_LEVEL` | `INFO` | Logging verbosity |
| `CORS_ORIGINS` | `*` | Comma-separated allowed origins |
| `REDIS_URL` | `memory://` | Rate limiter storage |
| `DOWNLOAD_WORK_DIR` | `/tmp/.download_tmp` | Temp download directory |
| `WORKSPACE_CLEANUP_INTERVAL_SECONDS` | `300` | Cleanup interval |
| `WORK_DIR_TTL_SECONDS` | `3600` | Temp dir lifetime |
| `DOWNLOAD_SOCKET_TIMEOUT` | `35` | yt-dlp socket timeout |
| `DOWNLOAD_RETRIES` | `3` | yt-dlp download retries |
| `DOWNLOAD_FRAGMENT_RETRIES` | `3` | yt-dlp fragment retries |
| `YTDLP_FORCE_IPV4` | `1` | Force IPv4 for yt-dlp |
| `YTDLP_COOKIES_BROWSER` | — | Browser to pull cookies from |
| `YTDLP_COOKIES_FILE` | — | Path to cookies file |

---

## Running Tests

```bash
python -m unittest discover -s tests -v
```

Tests mock yt-dlp — no network access needed.

---

## Troubleshooting

**TikTok download failed**  
Run `pip install -U yt-dlp`, restart Flask, and try again.

**Rate limit exceeded**  
Wait 60 seconds before retrying.

**"This video is private or requires login"**  
Use public content, or pass cookies via `YTDLP_COOKIES_FILE` or `YTDLP_COOKIES_BROWSER`.

**Metadata loads but format options are limited**  
Some platforms restrict format listings. Download with `best` quality as fallback.

**YouTube bot detection error**  
YouTube sometimes blocks datacenter IPs. Try passing browser cookies via the `YTDLP_COOKIES_BROWSER` env variable.

---

## Limitations

- Facebook and Instagram work best with public content
- YouTube on serverless deployments may require cookies to bypass bot detection
- Download rate limit is 5 per minute per IP

---

## License

MIT
