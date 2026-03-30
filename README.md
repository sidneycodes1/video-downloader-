# Multi-Platform Video Downloader

<!-- # CHANGED: README now reflects the current live API and backend behavior exactly. -->

A Flask web app that accepts a supported social-video URL, returns metadata/format options, and streams a downloadable file.

## Supported Platforms

- YouTube
- TikTok
- Instagram
- Facebook
- X (Twitter)

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

Open:

- http://127.0.0.1:5000

## Running Locally

```bash
python -m venv venv
# Windows PowerShell:
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
pip install -U yt-dlp
python vid.py
```

## API

<!-- # CHANGED: removed outdated mode/job_id/status API sections. -->

### `GET /api/health`

Returns service status plus key exposed endpoints.

Example response:

```json
{
  "status": "ok",
  "endpoints": ["/api/health", "/api/download", "/api/metadata", "/api/schedule", "/api/debug/ydlp-version", "/history", "/schedule"],
  "supported_platforms": ["facebook", "instagram", "tiktok", "twitter", "youtube"]
}
```

### `GET /api/debug/ydlp-version`

Returns installed `yt-dlp` version to help diagnose TikTok extractor compatibility.

Example response:

```json
{
  "yt_dlp_version": "2025.02.19",
  "tiktok_status": "ok"
}
```

### `POST /api/metadata`

Inspects a URL with `yt-dlp` (`download=false`) and returns title/thumbnail/duration/uploader plus selectable formats.

Request body:

```json
{
  "url": "https://www.youtube.com/watch?v=abc123"
}
```

Success response (shape):

```json
{
  "title": "Example Video",
  "thumbnail": "https://example.com/thumb.jpg",
  "duration": 187,
  "uploader": "Example Channel",
  "platform": "youtube",
  "formats": [
    {
      "format_id": "22",
      "ext": "mp4",
      "height": 720,
      "filesize": 7000000,
      "label": "720p MP4"
    },
    {
      "format_id": "audio-only",
      "ext": "mp3",
      "height": null,
      "filesize": null,
      "label": "Audio only (MP3)"
    }
  ]
}
```

Error response:

```json
{
  "error": "Human-readable error message"
}
```

### `POST /api/download`

Downloads and streams the media file as an attachment.

Request body:

```json
{
  "url": "https://www.youtube.com/watch?v=abc123",
  "platform": "YouTube",
  "download_type": "video",
  "quality": "best",
  "format_id": "22"
}
```

Notes:

- `platform` is optional as a hint. Backend auto-detects from URL.
- `download_type` must be `video` or `audio`.
- `quality` must be one of `best`, `1080`, `720`, `480`, `360`.
- `format_id` is optional and must be alphanumeric with `+` or `-` only (max 20 chars).
- On success, response is file stream with:
  - `Content-Disposition: attachment; ...`
  - `Cache-Control: no-store`

Error response:

```json
{
  "error": "Human-readable error message"
}
```

## Security and Stability Controls

- URL scheme and hostname validation (`http`/`https` only)
- Local/private network URL rejection
- Platform domain allowlist
- Request payload size limit (small JSON body only)
- CORS configurable for `/api/*`
- Temporary per-download work directories with automatic cleanup
- Stale workspace cleanup at startup and periodically during runtime

## Rate Limiting

<!-- # CHANGED: documents actual limiter behavior in live code. -->

- `POST /api/download`: `5` successful downloads per minute per client IP
- `POST /api/metadata`: `5` requests per minute per client IP
- On limit breach, API returns:

```json
{
  "error": "Rate limit exceeded",
  "retry_after": 60
}
```

## Configuration (Environment Variables)

- `PORT` (default: `5000`)
- `FLASK_DEBUG` (`1` or `0`)
- `LOG_LEVEL` (default: `INFO`)
- `CORS_ORIGINS` (default: `*`; comma-separated for restricted origins)
- `REDIS_URL` (optional; limiter storage, defaults to `memory://`)
- `DOWNLOAD_WORK_DIR` (default: `./.download_tmp`)
- `WORKSPACE_CLEANUP_INTERVAL_SECONDS` (default: `300`)
- `WORK_DIR_TTL_SECONDS` (default: `3600`)
- `DOWNLOAD_SOCKET_TIMEOUT` (default: `35`)
- `DOWNLOAD_RETRIES` (default: `3`)
- `DOWNLOAD_FRAGMENT_RETRIES` (default: `3`)
- `YTDLP_FORCE_IPV4` (default: `1`; set `0` to disable)
- `YTDLP_COOKIES_BROWSER` (optional)
- `YTDLP_COOKIES_FILE` (optional)

## Limitations

<!-- # CHANGED: added requested MVP limitations. -->

- Facebook downloads currently work best with public videos.
- Instagram downloads currently work best with public posts/reels.
- TikTok downloads currently work best with public videos.
- Download rate limit is 5 successful downloads/minute per IP.

## Troubleshooting

1. `TikTok download failed` or extractor errors
   - Run `pip install -U yt-dlp`, restart Flask, and test again.
2. `Rate limit exceeded` or platform 429
   - Wait 1-2 minutes before retrying.
3. `Local or private network URLs are not allowed`
   - Use a public `http://` or `https://` social video URL.
4. `This video is private or requires login`
   - Use public content or provide supported cookies where applicable.
5. Metadata succeeds but format options are limited
   - Continue with `best` format; some platforms hide full variant lists.

## Tests

Run backend tests:

```bash
python -m unittest discover -s tests -v
```

Tests mock `yt_dlp`, so they run without external network access.
