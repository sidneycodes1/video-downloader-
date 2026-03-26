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
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "1" if default else "0").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    logger.warning("Invalid boolean for %s=%s. Falling back to %s.", name, raw, default)
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

DOWNLOAD_RATE_LIMIT = "5 per minute"
RATE_LIMIT_STORAGE_URI = os.getenv("REDIS_URL", "").strip() or "memory://"
WORKSPACE_CLEANUP_INTERVAL_SECONDS = _env_int("WORKSPACE_CLEANUP_INTERVAL_SECONDS", 300)
WORK_DIR_TTL_SECONDS = _env_int("WORK_DIR_TTL_SECONDS", 3600)
LAST_WORKSPACE_CLEANUP = 0.0
WORKSPACE_CLEANUP_LOCK = threading.Lock()

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

METADATA_ALLOWED_EXTENSIONS = {"mp4", "webm", "mkv"}
ENDPOINTS_LIST = ["/api/health", "/api/download", "/api/metadata", "/history"]
FACEBOOK_QUERY_PARAMS_TO_DROP = {"mibextid", "ref", "refsrc", "sfnsn", "__tn__"}


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
    format_id: str | None


@dataclass(frozen=True)
class MetadataRequest:
    url: str
    platform: str


def normalize_platform(platform: str | None) -> str:
    if not platform:
        return ""
    return PLATFORM_ALIASES.get(platform.strip().lower(), platform.strip().lower())


def sanitize_filename(name: str) -> str:
    collapsed = re.sub(r"\s+", " ", name).strip()
    safe = re.sub(r"[^A-Za-z0-9._ -]+", "_", collapsed)
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


def normalize_facebook_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return raw_url

    if hostname == "facebook.com" or hostname.endswith(".facebook.com"):
        normalized_host = "www.facebook.com"
    elif hostname == "fb.watch" or hostname.endswith(".fb.watch"):
        normalized_host = "fb.watch"
    else:
        return raw_url

    path = parsed.path or "/"
    query_items = parse_qsl(parsed.query, keep_blank_values=False)
    filtered_query = [
        (key, value) for key, value in query_items if key.strip().lower() not in FACEBOOK_QUERY_PARAMS_TO_DROP
    ]
    filtered_query_map = dict(filtered_query)

    share_video_match = re.fullmatch(r"/share/v/([0-9]+)/?", path, flags=re.IGNORECASE)
    if share_video_match:
        filtered_query_map["v"] = share_video_match.group(1)
        path = "/watch/"

    reel_match = re.fullmatch(r"/reel/([0-9]+)/?", path, flags=re.IGNORECASE)
    if reel_match:
        filtered_query_map["v"] = reel_match.group(1)
        path = "/watch/"

    normalized_query = urlencode(filtered_query_map, doseq=True)
    return urlunparse(("https", normalized_host, path, "", normalized_query, ""))


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


def validate_supported_url(raw_url: str) -> tuple[str, str]:
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

    normalized_url = raw_url
    if detected_platform == "facebook":
        normalized_url = normalize_facebook_url(raw_url)

    return normalized_url, detected_platform


def parse_download_payload() -> DownloadRequest:
    if not request.is_json:
        raise APIError("Request body must be JSON.", 400)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise APIError("Malformed JSON payload.", 400)

    raw_url = str(data.get("url", "")).strip()
    url, detected_platform = validate_supported_url(raw_url)

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
    if raw_format_id:
        if len(raw_format_id) > 20 or not re.fullmatch(r"[A-Za-z0-9+-]+", raw_format_id):
            raise APIError("format_id is invalid. Use only letters, numbers, +, -, max 20 chars.", 422)

    return DownloadRequest(
        url=url,
        platform=detected_platform,
        download_type=raw_type,
        quality=raw_quality,
        format_id=raw_format_id or None,
    )


def parse_metadata_payload() -> MetadataRequest:
    if not request.is_json:
        raise APIError("Request body must be JSON.", 400)

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise APIError("Malformed JSON payload.", 400)

    raw_url = str(data.get("url", "")).strip()
    url, detected_platform = validate_supported_url(raw_url)
    return MetadataRequest(url=url, platform=detected_platform)


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


def normalize_info_payload(info: dict | list | None):
    if isinstance(info, dict) and info.get("entries"):
        first_entry = next((entry for entry in info["entries"] if entry), None)
        return first_entry or info
    return info


def build_metadata_formats(info: dict) -> list[dict]:
    raw_formats = info.get("formats")
    if not isinstance(raw_formats, list):
        raw_formats = []

    collected: list[dict] = []
    seen: set[str] = set()

    for current in raw_formats:
        if not isinstance(current, dict):
            continue

        format_id = str(current.get("format_id", "")).strip()
        if not format_id or format_id in seen:
            continue

        ext = str(current.get("ext", "")).strip().lower()
        if ext not in METADATA_ALLOWED_EXTENSIONS:
            continue

        vcodec = str(current.get("vcodec", "none")).lower()
        acodec = str(current.get("acodec", "none")).lower()
        has_video = vcodec not in {"none", ""}
        has_audio = acodec not in {"none", ""}
        if not has_video:
            continue

        raw_height = current.get("height")
        try:
            height = int(raw_height) if raw_height is not None else None
        except (TypeError, ValueError):
            height = None

        if not has_audio:
            if height is None or height < 144:
                continue

        raw_filesize = current.get("filesize") or current.get("filesize_approx")
        try:
            filesize = int(raw_filesize) if raw_filesize else None
        except (TypeError, ValueError):
            filesize = None

        if height:
            label = f"{height}p {ext.upper()}"
        else:
            label = f"{ext.upper()} video"

        collected.append(
            {
                "format_id": format_id,
                "ext": ext,
                "height": height,
                "filesize": filesize,
                "label": label,
                "sort_height": height or 0,
            }
        )
        seen.add(format_id)

    collected.sort(key=lambda item: (item["sort_height"], item["filesize"] or 0), reverse=True)
    for item in collected:
        item.pop("sort_height", None)

    collected.append(
        {
            "format_id": "audio-only",
            "ext": "mp3",
            "height": None,
            "filesize": None,
            "label": "Audio only (MP3)",
        }
    )
    return collected


def build_ydl_options(job: DownloadRequest, work_dir: Path) -> dict:
    if job.format_id == "audio-only":
        selected_format = "bestaudio/best"
    elif job.format_id:
        selected_format = job.format_id
    else:
        selected_format = build_format_selector(job.download_type, job.quality)

    opts = {
        "format": selected_format,
        "outtmpl": str(work_dir / "%(id)s.%(ext)s"),
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

    if job.format_id == "audio-only":
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    if job.platform == "youtube":
        opts["extractor_args"] = {"youtube": {"player_client": ["android", "web"]}}

    if _env_bool("YTDLP_FORCE_IPV4", True):
        # Some hosts block IPv6/mixed sockets; forcing IPv4 avoids common WinError 10013 cases.
        opts["source_address"] = "0.0.0.0"

    browser = os.getenv("YTDLP_COOKIES_BROWSER", "").strip().lower()
    cookie_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()
    if browser:
        opts["cookiesfrombrowser"] = (browser,)
    if cookie_file:
        opts["cookiefile"] = cookie_file

    return opts


def build_metadata_ydl_options(job: MetadataRequest) -> dict:
    opts = {
        "skip_download": True,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "geo_bypass": True,
        "socket_timeout": _env_int("DOWNLOAD_SOCKET_TIMEOUT", 35),
        "retries": _env_int("DOWNLOAD_RETRIES", 3),
        "fragment_retries": _env_int("DOWNLOAD_FRAGMENT_RETRIES", 3),
        "http_headers": build_headers(job.platform),
    }

    if job.platform == "youtube":
        opts["extractor_args"] = {"youtube": {"player_client": ["android", "web"]}}

    if _env_bool("YTDLP_FORCE_IPV4", True):
        opts["source_address"] = "0.0.0.0"

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
    if (
        "winerror 10013" in message
        or "getaddrinfo failed" in message
        or "failed to resolve" in message
        or "name or service not known" in message
        or "temporary failure in name resolution" in message
        or "nodename nor servname provided" in message
    ):
        return APIError(
            "Could not reach the source platform from this server (DNS/socket blocked). "
            "Check firewall, DNS, or VPN settings and try again.",
            502,
        )
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


@app.route("/history")
def history_page():
    return render_template("history.html")


@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify(
        {
            "status": "ok",
            "endpoints": ENDPOINTS_LIST,
            "supported_platforms": sorted(SUPPORTED_PLATFORM_DOMAINS.keys()),
        }
    )


@app.route("/api/metadata", methods=["POST"])
@limiter.limit(DOWNLOAD_RATE_LIMIT)
def video_metadata():
    try:
        job = parse_metadata_payload()
        ydl_opts = build_metadata_ydl_options(job)

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(job.url, download=False)

        info = normalize_info_payload(info)
        if not isinstance(info, dict):
            raise APIError("Unable to inspect this link for metadata.", 422)

        formats = build_metadata_formats(info)
        if not formats:
            raise APIError("No compatible formats were found for this URL.", 422)

        duration_value = info.get("duration")
        try:
            duration = int(duration_value) if duration_value is not None else None
        except (TypeError, ValueError):
            duration = None

        return jsonify(
            {
                "title": str(info.get("title", "Untitled video")),
                "thumbnail": info.get("thumbnail"),
                "duration": duration,
                "uploader": str(info.get("uploader") or info.get("channel") or "Unknown uploader"),
                "platform": job.platform,
                "formats": formats,
            }
        )

    except APIError as error:
        return jsonify({"error": error.message}), error.status_code
    except DownloadError as error:
        mapped_error = map_download_error(error)
        return jsonify({"error": mapped_error.message}), mapped_error.status_code
    except Exception as error:
        logger.exception("Unhandled metadata error")
        mapped_error = map_download_error(error)
        return jsonify({"error": mapped_error.message}), mapped_error.status_code


@app.route("/api/download", methods=["POST"])
@limiter.limit(DOWNLOAD_RATE_LIMIT, deduct_when=should_count_successful_download)
def download_video():
    work_dir: Path | None = None

    try:
        maybe_cleanup_stale_work_dirs()
        job = parse_download_payload()
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

        response = send_file(
            media_path,
            as_attachment=True,
            download_name=download_name,
            conditional=False,
        )
        response.call_on_close(lambda: cleanup_work_dir(work_dir))
        return response

    except APIError as error:
        cleanup_work_dir(work_dir)
        return jsonify({"error": error.message}), error.status_code
    except DownloadError as error:
        cleanup_work_dir(work_dir)
        mapped_error = map_download_error(error)
        logger.warning("DownloadError: %s", error)
        return jsonify({"error": mapped_error.message}), mapped_error.status_code
    except Exception as error:
        cleanup_work_dir(work_dir)
        logger.exception("Unhandled download error")
        mapped_error = map_download_error(error)
        return jsonify({"error": mapped_error.message}), mapped_error.status_code


@app.errorhandler(413)
def payload_too_large(_error):
    return jsonify({"error": "Request payload too large."}), 413


@app.errorhandler(429)
def rate_limit_exceeded(error):
    retry_after_value = getattr(error, "retry_after", None)
    try:
        retry_after = int(float(retry_after_value)) if retry_after_value is not None else 60
    except (TypeError, ValueError):
        retry_after = 60
    retry_after = max(1, retry_after)

    return jsonify({"error": "Rate limit exceeded", "retry_after": retry_after}), 429


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
