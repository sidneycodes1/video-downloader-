from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from flask import Flask, jsonify, make_response, render_template, request, send_file
from flask_cors import CORS
from werkzeug.exceptions import TooManyRequests
from yt_dlp.utils import DownloadError

try:
    from flask_limiter import Limiter  # type: ignore
    from flask_limiter.errors import RateLimitExceeded  # type: ignore

    FLASK_LIMITER_AVAILABLE = True
except ModuleNotFoundError:
    FLASK_LIMITER_AVAILABLE = False

    class RateLimitExceeded(TooManyRequests):
        def __init__(self, retry_after: int | None = None):
            super().__init__(description="Too Many Requests")
            self.retry_after = retry_after

    class Limiter:  # Minimal fallback for offline/dev environments.
        def __init__(self, key_func, app=None, storage_uri: str = "memory://", default_limits=None):
            self.key_func = key_func
            self.storage_uri = storage_uri
            self.default_limits = default_limits or []
            self._buckets: dict[tuple[str, str, int], list[float]] = {}
            self._lock = threading.Lock()
            if app is not None:
                self.init_app(app)

        def init_app(self, app):
            self.app = app
            return app

        def reset(self):
            with self._lock:
                self._buckets.clear()

        @staticmethod
        def _parse_limit(limit_value: str) -> tuple[int, int]:
            match = re.fullmatch(r"\s*(\d+)\s+per\s+minute\s*", limit_value.lower())
            if not match:
                raise ValueError(f"Unsupported fallback limit format: {limit_value}")
            return int(match.group(1)), 60

        def limit(self, limit_value: str, deduct_when=None):
            max_requests, window_seconds = self._parse_limit(limit_value)

            def decorator(func):
                @wraps(func)
                def wrapped(*args, **kwargs):
                    key = str(self.key_func())
                    bucket_key = (func.__name__, key, window_seconds)
                    now = time.time()

                    with self._lock:
                        bucket = self._buckets.setdefault(bucket_key, [])
                        bucket[:] = [stamp for stamp in bucket if (now - stamp) < window_seconds]
                        if len(bucket) >= max_requests:
                            retry_after = max(1, int(window_seconds - (now - bucket[0])))
                            raise RateLimitExceeded(retry_after=retry_after)

                    result = func(*args, **kwargs)
                    response = make_response(result)

                    should_deduct = True
                    if deduct_when is not None:
                        try:
                            should_deduct = bool(deduct_when(response))
                        except Exception:
                            should_deduct = True

                    if should_deduct:
                        with self._lock:
                            bucket = self._buckets.setdefault(bucket_key, [])
                            now = time.time()
                            bucket[:] = [stamp for stamp in bucket if (now - stamp) < window_seconds]
                            bucket.append(now)

                    return response

                return wrapped

            return decorator

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("video_downloader")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # Requests are tiny JSON payloads.

cors_origins_env = os.getenv("CORS_ORIGINS", "*").strip()
if cors_origins_env == "*":
    CORS(app, resources={r"/api/*": {"origins": "*"}})
else:
    parsed_origins = [origin.strip() for origin in cors_origins_env.split(",") if origin.strip()]
    CORS(app, resources={r"/api/*": {"origins": parsed_origins}})


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid integer for %s=%s. Falling back to %s.", name, raw, default)
        return default


SUPPORTED_PLATFORM_DOMAINS: dict[str, tuple[str, ...]] = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "instagram": ("instagram.com", "instagr.am"),
    "tiktok": ("tiktok.com", "douyin.com"),
    "facebook": ("facebook.com", "fb.watch"),
    "twitter": ("x.com", "twitter.com", "t.co"),
}

PLATFORM_ALIASES = {
    "youtube": "youtube",
    "instagram": "instagram",
    "facebook": "facebook",
    "tiktok": "tiktok",
    "twitter": "twitter",
    "x": "twitter",
}

QUALITY_HEIGHTS: dict[str, int | None] = {
    "best": None,
    "1080": 1080,
    "720": 720,
    "480": 480,
    "360": 360,
}

WORKSPACE_CLEANUP_INTERVAL_SECONDS = _env_int("WORKSPACE_CLEANUP_INTERVAL_SECONDS", 300)
WORK_DIR_TTL_SECONDS = _env_int("WORK_DIR_TTL_SECONDS", 3600)
LAST_WORKSPACE_CLEANUP = 0.0
WORKSPACE_CLEANUP_LOCK = threading.Lock()
DOWNLOAD_PROGRESS_TTL_SECONDS = _env_int("DOWNLOAD_PROGRESS_TTL_SECONDS", 1800)
DOWNLOAD_PROGRESS_CLEANUP_INTERVAL_SECONDS = _env_int("DOWNLOAD_PROGRESS_CLEANUP_INTERVAL_SECONDS", 120)
LAST_DOWNLOAD_PROGRESS_CLEANUP = 0.0
DOWNLOAD_PROGRESS_CLEANUP_LOCK = threading.Lock()
DOWNLOAD_PROGRESS: dict[str, dict] = {}
DOWNLOAD_PROGRESS_LOCK = threading.Lock()
DOWNLOAD_RATE_LIMIT = "5 per minute"
RATE_LIMIT_STORAGE_URI = os.getenv("REDIS_URL", "").strip() or "memory://"

KNOWN_MEDIA_EXTENSIONS = {
    ".mp4",
    ".m4a",
    ".webm",
    ".mkv",
    ".mov",
    ".mp3",
    ".aac",
    ".ogg",
    ".wav",
    ".flac",
}


class APIError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class DownloadRequest:
    url: str
    platform: str
    download_type: str
    quality: str
    mode: str
    format_id: str | None
    job_id: str | None


def normalize_platform(platform: str | None) -> str:
    if not platform:
        return ""
    return PLATFORM_ALIASES.get(platform.strip().lower(), platform.strip().lower())


def sanitize_filename(name: str) -> str:
    collapsed = re.sub(r"\s+", " ", name).strip()
    safe = re.sub(r'[^A-Za-z0-9._ -]+', "_", collapsed)
    safe = safe.strip(". ")
    return safe[:120] or "video"


def extract_hostname(video_url: str) -> str:
    parsed = urlparse(video_url)
    if parsed.scheme not in {"http", "https"}:
        raise APIError("Only http:// and https:// links are supported.", 400)
    if not parsed.netloc:
        raise APIError("Invalid URL. Please paste a complete link.", 400)
    hostname = (parsed.hostname or "").strip().lower()
    if not hostname:
        raise APIError("Invalid URL. Hostname is missing.", 400)
    return hostname


def is_private_or_local_host(hostname: str) -> bool:
    if hostname in {"localhost"} or hostname.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def detect_platform(hostname: str) -> str | None:
    for platform, domains in SUPPORTED_PLATFORM_DOMAINS.items():
        for domain in domains:
            if hostname == domain or hostname.endswith(f".{domain}"):
                return platform
    return None


def get_client_ip() -> str:
    forwarded_for = request.headers.get("X-Forwarded-For", "")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    return request.remote_addr or "unknown"


limiter = Limiter(
    key_func=get_client_ip,
    app=app,
    storage_uri=RATE_LIMIT_STORAGE_URI,
    default_limits=[],
)

if not FLASK_LIMITER_AVAILABLE:
    logger.warning("flask-limiter is unavailable. Falling back to in-process limiter.")


def parse_download_payload() -> DownloadRequest:
    if not request.is_json:
        raise APIError("Request body must be JSON.", 400)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise APIError("Malformed JSON payload.", 400)

    raw_url = str(data.get("url", "")).strip()
    if not raw_url:
        raise APIError("Please provide a video URL.", 400)
    if len(raw_url) > 2048:
        raise APIError("URL is too long.", 400)

    hostname = extract_hostname(raw_url)
    if is_private_or_local_host(hostname):
        raise APIError("Local or private network URLs are not allowed.", 400)

    detected_platform = detect_platform(hostname)
    if not detected_platform:
        raise APIError(
            "Unsupported platform. Use a YouTube, TikTok, Instagram, Facebook, or X link.",
            400,
        )

    platform_hint = normalize_platform(data.get("platform"))
    if platform_hint and platform_hint != detected_platform:
        logger.info(
            "Platform hint '%s' did not match detected platform '%s'. Using detected platform.",
            platform_hint,
            detected_platform,
        )

    raw_type = str(data.get("download_type", "video")).strip().lower()
    if raw_type not in {"video", "audio"}:
        raise APIError("download_type must be either 'video' or 'audio'.", 400)

    raw_quality = str(data.get("quality", "best")).strip().lower().replace("p", "")
    if raw_quality not in QUALITY_HEIGHTS:
        valid_values = ", ".join(QUALITY_HEIGHTS.keys())
        raise APIError(f"quality must be one of: {valid_values}.", 400)

    raw_format_id = str(data.get("format_id", "")).strip()
    if len(raw_format_id) > 200:
        raise APIError("format_id is too long.", 400)

    raw_mode = str(data.get("mode", "")).strip().lower()
    mode = raw_mode or ("download" if raw_format_id else "formats")
    if mode not in {"formats", "download"}:
        raise APIError("mode must be either 'formats' or 'download'.", 400)
    if mode == "download" and not raw_format_id:
        raise APIError("format_id is required when mode is 'download'.", 400)

    raw_job_id = str(data.get("job_id", "")).strip()
    if raw_job_id and len(raw_job_id) > 120:
        raise APIError("job_id is too long.", 400)
    if raw_job_id and not re.fullmatch(r"[A-Za-z0-9._-]+", raw_job_id):
        raise APIError("job_id contains invalid characters.", 400)

    job_id = raw_job_id or (f"job_{uuid.uuid4().hex}" if mode == "download" else None)

    return DownloadRequest(
        url=raw_url,
        platform=detected_platform,
        download_type=raw_type,
        quality=raw_quality,
        mode=mode,
        format_id=raw_format_id or None,
        job_id=job_id,
    )


def build_headers(platform: str) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/123.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "en-US,en;q=0.9",
    }
    if platform == "instagram":
        headers["Referer"] = "https://www.instagram.com/"
    elif platform == "tiktok":
        headers["Referer"] = "https://www.tiktok.com/"
    elif platform == "facebook":
        headers["Referer"] = "https://www.facebook.com/"
    elif platform == "twitter":
        headers["Referer"] = "https://x.com/"
    return headers


def build_format_selector(download_type: str, quality: str) -> str:
    if download_type == "audio":
        return "bestaudio[ext=m4a]/bestaudio/best"

    max_height = QUALITY_HEIGHTS.get(quality)
    if max_height is None:
        return "best[ext=mp4]/best"
    return f"best[height<={max_height}][ext=mp4]/best[height<={max_height}]/best"


def should_count_successful_download(response) -> bool:
    if response.status_code != 200:
        return False
    content_disposition = response.headers.get("Content-Disposition", "").lower()
    return "attachment" in content_disposition


def humanize_filesize(size_bytes: int | None) -> str | None:
    if not size_bytes or size_bytes <= 0:
        return None

    size_value = float(size_bytes)
    units = ["B", "KB", "MB", "GB", "TB"]
    unit_index = 0

    while size_value >= 1024 and unit_index < len(units) - 1:
        size_value /= 1024
        unit_index += 1

    if unit_index == 0:
        return f"{int(size_value)} {units[unit_index]}"
    return f"{size_value:.1f} {units[unit_index]}"


def normalize_info_payload(info: dict | list | None):
    if isinstance(info, dict) and info.get("entries"):
        first_entry = next((entry for entry in info["entries"] if entry), None)
        return first_entry or info
    return info


def extract_available_formats(info: dict, download_type: str) -> list[dict]:
    raw_formats = info.get("formats")
    if not isinstance(raw_formats, list):
        return []

    available_formats: list[dict] = []
    seen_ids: set[str] = set()

    for item in raw_formats:
        if not isinstance(item, dict):
            continue

        format_id = str(item.get("format_id", "")).strip()
        if not format_id or format_id in seen_ids:
            continue

        vcodec = str(item.get("vcodec", "none")).lower()
        acodec = str(item.get("acodec", "none")).lower()
        has_video = vcodec not in {"none", ""}
        has_audio = acodec not in {"none", ""}

        if download_type == "audio":
            if not has_audio:
                continue
        else:
            # Prefer ready-to-play single-file streams to avoid no-audio downloads.
            if not (has_video and has_audio):
                continue

        raw_height = item.get("height")
        try:
            height = int(raw_height) if raw_height else None
        except (TypeError, ValueError):
            height = None

        if download_type == "audio":
            resolution = "audio"
        elif height:
            resolution = f"{height}p"
        else:
            resolution = str(item.get("resolution") or item.get("format_note") or "unknown").strip()

        raw_size = item.get("filesize") or item.get("filesize_approx")
        try:
            size_bytes = int(raw_size) if raw_size else None
        except (TypeError, ValueError):
            size_bytes = None

        available_formats.append(
            {
                "format_id": format_id,
                "resolution": resolution,
                "ext": str(item.get("ext", "unknown")).strip().lower(),
                "filesize": humanize_filesize(size_bytes),
                "filesize_bytes": size_bytes,
                "height_sort": height or 0,
            }
        )
        seen_ids.add(format_id)

    available_formats.sort(
        key=lambda current: (current["height_sort"], current["filesize_bytes"] or 0),
        reverse=True,
    )

    for current in available_formats:
        current.pop("height_sort", None)

    return available_formats


def set_download_progress(
    job_id: str | None,
    *,
    status: str,
    percent: float | None = None,
    message: str | None = None,
    downloaded_bytes: int | None = None,
    total_bytes: int | None = None,
) -> None:
    if not job_id:
        return

    payload = {
        "job_id": job_id,
        "status": status,
        "updated_at": int(time.time()),
    }

    if percent is not None:
        payload["percent"] = max(0.0, min(100.0, round(float(percent), 2)))
    if message:
        payload["message"] = message
    if downloaded_bytes is not None:
        payload["downloaded_bytes"] = int(downloaded_bytes)
    if total_bytes is not None:
        payload["total_bytes"] = int(total_bytes)

    with DOWNLOAD_PROGRESS_LOCK:
        merged = DOWNLOAD_PROGRESS.get(job_id, {}).copy()
        merged.update(payload)
        DOWNLOAD_PROGRESS[job_id] = merged


def get_download_progress(job_id: str) -> dict | None:
    with DOWNLOAD_PROGRESS_LOCK:
        stored = DOWNLOAD_PROGRESS.get(job_id)
        return stored.copy() if stored else None


def maybe_cleanup_stale_download_progress() -> None:
    global LAST_DOWNLOAD_PROGRESS_CLEANUP
    now = time.time()

    if (now - LAST_DOWNLOAD_PROGRESS_CLEANUP) < DOWNLOAD_PROGRESS_CLEANUP_INTERVAL_SECONDS:
        return

    with DOWNLOAD_PROGRESS_CLEANUP_LOCK:
        now = time.time()
        if (now - LAST_DOWNLOAD_PROGRESS_CLEANUP) < DOWNLOAD_PROGRESS_CLEANUP_INTERVAL_SECONDS:
            return

        stale_cutoff = now - DOWNLOAD_PROGRESS_TTL_SECONDS
        with DOWNLOAD_PROGRESS_LOCK:
            stale_job_ids = [
                job_id
                for job_id, entry in DOWNLOAD_PROGRESS.items()
                if int(entry.get("updated_at", 0)) < stale_cutoff
            ]
            for job_id in stale_job_ids:
                DOWNLOAD_PROGRESS.pop(job_id, None)

        LAST_DOWNLOAD_PROGRESS_CLEANUP = now


def build_progress_hook(job_id: str):
    def _progress_hook(update: dict) -> None:
        status = str(update.get("status", "")).lower()

        if status == "downloading":
            total_bytes = update.get("total_bytes") or update.get("total_bytes_estimate")
            downloaded_bytes = update.get("downloaded_bytes") or 0

            progress_percent: float | None = None
            if isinstance(total_bytes, (int, float)) and total_bytes > 0:
                progress_percent = (float(downloaded_bytes) / float(total_bytes)) * 100.0

            percent_label = str(update.get("_percent_str", "")).strip()
            message = f"Downloading {percent_label}" if percent_label else "Downloading..."

            set_download_progress(
                job_id,
                status="downloading",
                percent=progress_percent,
                message=message,
                downloaded_bytes=int(downloaded_bytes),
                total_bytes=int(total_bytes) if isinstance(total_bytes, (int, float)) else None,
            )
        elif status == "finished":
            set_download_progress(
                job_id,
                status="processing",
                percent=99.0,
                message="Finalizing file...",
            )

    return _progress_hook


def build_ydl_options(job: DownloadRequest, work_dir: Path | None = None, *, probe_only: bool = False) -> dict:
    selected_format = job.format_id if job.format_id else build_format_selector(job.download_type, job.quality)

    opts = {
        "format": selected_format,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "socket_timeout": _env_int("DOWNLOAD_SOCKET_TIMEOUT", 35),
        "retries": _env_int("DOWNLOAD_RETRIES", 3),
        "fragment_retries": _env_int("DOWNLOAD_FRAGMENT_RETRIES", 3),
        "http_headers": build_headers(job.platform),
    }

    if probe_only:
        opts["skip_download"] = True
    else:
        if work_dir is None:
            raise APIError("Download workspace was not initialized.", 500)
        opts["outtmpl"] = str(work_dir / "%(id)s.%(ext)s")
        if job.job_id:
            opts["progress_hooks"] = [build_progress_hook(job.job_id)]

    if job.platform == "youtube":
        opts["extractor_args"] = {"youtube": {"player_client": ["android", "web"]}}

    browser = os.getenv("YTDLP_COOKIES_BROWSER", "").strip().lower()
    cookie_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    if cookie_file:
        opts["cookiefile"] = cookie_file

    return opts


def pick_media_file(work_dir: Path) -> Path:
    candidates = [
        path
        for path in work_dir.iterdir()
        if path.is_file() and path.suffix.lower() in KNOWN_MEDIA_EXTENSIONS
    ]
    if not candidates:
        raise APIError("Download failed before a media file was created.", 500)
    return max(candidates, key=lambda file_path: file_path.stat().st_size)


def map_download_error(error: Exception) -> APIError:
    message = str(error).lower()

    if "unsupported url" in message or "no suitable extractor" in message:
        return APIError("Unsupported link. Please paste a valid supported video URL.", 400)
    if "private" in message or "sign in" in message or "login" in message:
        return APIError("This video is private or requires login.", 403)
    if "429" in message or "too many requests" in message or "rate limit" in message:
        return APIError("Source platform rate limit reached. Please try again later.", 429)
    if "404" in message or "not found" in message or "unavailable" in message:
        return APIError("Video not found or unavailable.", 404)
    if "timed out" in message or "timeout" in message:
        return APIError("Download timed out. Please try again.", 504)
    if "network" in message or "connection" in message:
        return APIError("Network error while downloading. Please try again.", 502)

    return APIError("Download failed. Please try a different link.", 500)


def cleanup_work_dir(work_dir: Path | None) -> None:
    if not work_dir:
        return
    shutil.rmtree(work_dir, ignore_errors=True)


def get_download_workspace() -> Path:
    configured_path = os.getenv("DOWNLOAD_WORK_DIR", "").strip()
    if configured_path:
        base_path = Path(configured_path)
    else:
        base_path = Path(__file__).resolve().parent / ".download_tmp"
    base_path.mkdir(parents=True, exist_ok=True)
    return base_path


def create_work_dir() -> Path:
    base_path = get_download_workspace()
    for _ in range(5):
        candidate = base_path / f"video_dl_{uuid.uuid4().hex}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise APIError("Unable to allocate a temporary working directory.", 500)


def maybe_cleanup_stale_work_dirs() -> None:
    global LAST_WORKSPACE_CLEANUP
    now = time.time()

    if (now - LAST_WORKSPACE_CLEANUP) < WORKSPACE_CLEANUP_INTERVAL_SECONDS:
        return

    with WORKSPACE_CLEANUP_LOCK:
        now = time.time()
        if (now - LAST_WORKSPACE_CLEANUP) < WORKSPACE_CLEANUP_INTERVAL_SECONDS:
            return

        cutoff_timestamp = now - WORK_DIR_TTL_SECONDS
        workspace = get_download_workspace()
        for candidate in workspace.glob("video_dl_*"):
            try:
                if candidate.is_dir() and candidate.stat().st_mtime < cutoff_timestamp:
                    shutil.rmtree(candidate, ignore_errors=True)
            except OSError:
                continue

        LAST_WORKSPACE_CLEANUP = now


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/download")
def download_page():
    return render_template("download.html")


@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify(
        {
            "status": "ok",
            "supported_platforms": sorted(SUPPORTED_PLATFORM_DOMAINS.keys()),
        }
    )


@app.route("/api/download", methods=["POST"])
@limiter.limit(DOWNLOAD_RATE_LIMIT, deduct_when=should_count_successful_download)
def download_video():
    work_dir: Path | None = None
    job_id: str | None = None

    try:
        maybe_cleanup_stale_work_dirs()
        maybe_cleanup_stale_download_progress()

        job = parse_download_payload()
        job_id = job.job_id

        if job.mode == "formats":
            logger.info(
                "Fetching formats platform=%s type=%s quality=%s",
                job.platform,
                job.download_type,
                job.quality,
            )
            probe_opts = build_ydl_options(job, probe_only=True)
            with yt_dlp.YoutubeDL(probe_opts) as ydl:
                info = ydl.extract_info(job.url, download=False)

            info = normalize_info_payload(info)
            if not isinstance(info, dict):
                raise APIError("Unable to inspect this link for downloadable formats.", 502)

            available_formats = extract_available_formats(info, job.download_type)
            if not available_formats:
                raise APIError("No downloadable formats were found for this link.", 404)

            return jsonify(
                {
                    "title": str(info.get("title", "video")),
                    "platform": job.platform,
                    "download_type": job.download_type,
                    "formats": available_formats,
                }
            )

        set_download_progress(job_id, status="starting", percent=0.0, message="Starting download...")
        work_dir = create_work_dir()
        ydl_opts = build_ydl_options(job, work_dir)

        logger.info(
            "Starting download platform=%s type=%s quality=%s format_id=%s",
            job.platform,
            job.download_type,
            job.quality,
            job.format_id,
        )

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(job.url, download=True)

        info = normalize_info_payload(info)

        media_path = pick_media_file(work_dir)
        title = sanitize_filename(str(info.get("title", "video"))) if isinstance(info, dict) else "video"
        download_name = f"{title}{media_path.suffix.lower()}"
        set_download_progress(job_id, status="finished", percent=100.0, message="Download complete.")

        response = send_file(
            media_path,
            as_attachment=True,
            download_name=download_name,
            conditional=False,
        )
        if job_id:
            response.headers["X-Download-Job-Id"] = job_id
        response.call_on_close(lambda: cleanup_work_dir(work_dir))
        return response

    except APIError as error:
        cleanup_work_dir(work_dir)
        set_download_progress(job_id, status="error", message=error.message)
        return jsonify({"error": error.message}), error.status_code
    except DownloadError as error:
        cleanup_work_dir(work_dir)
        mapped_error = map_download_error(error)
        set_download_progress(job_id, status="error", message=mapped_error.message)
        logger.warning("DownloadError: %s", error)
        return jsonify({"error": mapped_error.message}), mapped_error.status_code
    except Exception as error:
        cleanup_work_dir(work_dir)
        logger.exception("Unhandled download error")
        mapped_error = map_download_error(error)
        set_download_progress(job_id, status="error", message=mapped_error.message)
        return jsonify({"error": mapped_error.message}), mapped_error.status_code


@app.route("/api/download/status/<job_id>", methods=["GET"])
def download_status(job_id: str):
    maybe_cleanup_stale_download_progress()
    progress = get_download_progress(job_id)
    if not progress:
        return jsonify({"status": "not_found", "job_id": job_id}), 404
    return jsonify(progress)


@app.errorhandler(413)
def payload_too_large(_error):
    return jsonify({"error": "Request payload too large."}), 413


@app.errorhandler(429)
def rate_limit_exceeded(error):
    retry_after = None
    if isinstance(error, RateLimitExceeded):
        retry_after = getattr(error, "retry_after", None)

    payload = {"error": "Too many successful downloads. Please wait before trying again."}
    if retry_after is not None:
        try:
            payload["retry_after"] = int(float(retry_after))
        except (TypeError, ValueError):
            pass

    return jsonify(payload), 429


if __name__ == "__main__":
    port = _env_int("PORT", 5000)
    debug = os.getenv("FLASK_DEBUG", "0").strip() == "1"
    print("\n" + "=" * 50)
    print("Multi-Platform Video Downloader Starting")
    print("=" * 50)
    print(f"URL: http://127.0.0.1:{port}")
    print(f"Alternative: http://localhost:{port}")
    print("Supported platforms: YouTube, Facebook, TikTok, Twitter/X, Instagram")
    print("=" * 50 + "\n")
    app.run(debug=debug, host="0.0.0.0", port=port)
