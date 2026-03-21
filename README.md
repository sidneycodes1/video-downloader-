# Multi-Platform Video Downloader

A Flask web app that accepts a supported video URL and returns a downloadable media file.

## Supported Platforms

- YouTube
- TikTok
- Instagram
- Facebook
- X (Twitter)

## What This Version Fixes

- Stable end-to-end flow from input URL to downloadable response
- Strict URL validation and platform allowlisting
- Better error handling with clear API messages
- Request rate limiting to reduce abuse
- Safer temporary-file lifecycle and cleanup after response close
- Frontend download UX improvements (loading lock, robust error parsing, success feedback)

## Requirements

- Python 3.10+
- `pip`

## Install

```bash
pip install -r requirements.txt
```

## Run

```bash
python vid.py
```

Then open:

- http://127.0.0.1:5000

## API

### `POST /api/download`

This endpoint now works in two steps:

1) **Format discovery** (`mode: "formats"`): inspects the URL with `yt-dlp` (`download=false`) and returns quality options.
2) **Download** (`mode: "download"`): downloads the selected `format_id`.

Format discovery request:

```json
{
  "mode": "formats",
  "url": "https://www.youtube.com/watch?v=...",
  "platform": "YouTube",
  "download_type": "video",
  "quality": "best"
}
```

Format discovery response:

```json
{
  "title": "Example Video",
  "platform": "youtube",
  "download_type": "video",
  "formats": [
    {
      "format_id": "22",
      "resolution": "720p",
      "ext": "mp4",
      "filesize": "7.0 MB",
      "filesize_bytes": 7000000
    }
  ]
}
```

Download request:

```json
{
  "mode": "download",
  "url": "https://www.youtube.com/watch?v=...",
  "platform": "YouTube",
  "download_type": "video",
  "format_id": "22",
  "job_id": "job_123"
}
```

Download success:

- Returns file stream with `Content-Disposition: attachment`.

Notes:

- `platform` is optional as a hint. Backend auto-detects platform from URL.
- `download_type` supports `video` or `audio`.
- `quality` supports `best`, `1080`, `720`, `480`, `360`.
- `job_id` is optional; if omitted, backend generates one for download mode.

### `GET /api/health`

Health check endpoint.

### `GET /api/download/status/<job_id>`

Returns in-progress download status updates (percentage + message) for frontend polling.

## Security and Stability Controls

- URL scheme and hostname validation (`http`/`https` only)
- Local/private network URL rejection
- Platform domain allowlist
- Rate limiting by client IP (5 successful downloads per minute on `POST /api/download`)
- Request payload size limit
- Configurable CORS for `/api/*`

## Configuration (Environment Variables)

- `PORT` (default: `5000`)
- `FLASK_DEBUG` (`1` or `0`)
- `CORS_ORIGINS` (default: `*`, comma-separated for restricted origins)
- `REDIS_URL` (optional; if unset, limiter falls back to `memory://`)
- `DOWNLOAD_WORK_DIR` (default: `./.download_tmp`)
- `DOWNLOAD_SOCKET_TIMEOUT` (default: `35`)
- `DOWNLOAD_RETRIES` (default: `3`)
- `DOWNLOAD_FRAGMENT_RETRIES` (default: `3`)
- `DOWNLOAD_PROGRESS_TTL_SECONDS` (default: `1800`)
- `DOWNLOAD_PROGRESS_CLEANUP_INTERVAL_SECONDS` (default: `120`)
- `YTDLP_COOKIES_BROWSER` (optional)
- `YTDLP_COOKIES_FILE` (optional)

## Tests

Run backend tests:

```bash
python -m unittest discover -s tests -v
```

The tests mock `yt_dlp` so they run without external network access.
