from __future__ import annotations

import ipaddress
import json  # NEW FEATURE: Download Scheduler
import logging
import os
import re
import shutil
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError  # CHANGED
from datetime import datetime, timedelta, timezone  # NEW FEATURE: Download Scheduler
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import yt_dlp
from yt_dlp import version as yt_dlp_version  # FIXED: TIKTOK
from flask import Flask, Response, jsonify, make_response, render_template, request, send_file, stream_with_context
from flask_cors import CORS
from werkzeug.exceptions import TooManyRequests
from yt_dlp.utils import DownloadError

# TIKTOK TEST URLS (test these after applying fixes):  # FIXED: TIKTOK
# Short link:  https://vm.tiktok.com/ZMxxxxxxx/  # FIXED: TIKTOK
# Direct link: https://www.tiktok.com/@username/video/1234567890123456789  # FIXED: TIKTOK
# If both fail, run: pip install -U yt-dlp   then restart Flask  # FIXED: TIKTOK

try:
    from zoneinfo import ZoneInfo  # NEW FEATURE: Download Scheduler
except Exception:  # pragma: no cover - defensive fallback for unusual runtimes.
    ZoneInfo = None  # type: ignore

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


def get_yt_dlp_version() -> str:  # FIXED: TIKTOK
    return str(getattr(yt_dlp_version, "__version__", "unknown"))  # FIXED: TIKTOK


def get_yt_dlp_release_year(version_value: str) -> int | None:  # FIXED: TIKTOK
    match = re.match(r"^\s*(\d{4})", str(version_value or "").strip())  # FIXED: TIKTOK
    if not match:  # FIXED: TIKTOK
        return None  # FIXED: TIKTOK
    try:  # FIXED: TIKTOK
        return int(match.group(1))  # FIXED: TIKTOK
    except ValueError:  # FIXED: TIKTOK
        return None  # FIXED: TIKTOK


YTDLP_VERSION = get_yt_dlp_version()  # FIXED: TIKTOK
YTDLP_RELEASE_YEAR = get_yt_dlp_release_year(YTDLP_VERSION)  # FIXED: TIKTOK
logger.info("yt-dlp version detected: %s", YTDLP_VERSION)  # FIXED: TIKTOK # SE: startup version visibility
if YTDLP_RELEASE_YEAR is not None and YTDLP_RELEASE_YEAR < 2024:  # FIXED: TIKTOK
    logger.warning(  # FIXED: TIKTOK
        "yt-dlp version %s looks outdated for TikTok. Upgrade to 2024.x or newer.",  # FIXED: TIKTOK
        YTDLP_VERSION,  # FIXED: TIKTOK
    )  # FIXED: TIKTOK

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
RUNTIME_START_LOCK = threading.Lock()
RUNTIME_STARTED = False

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
ENDPOINTS_LIST = ["/api/health", "/api/download", "/api/metadata", "/api/schedule", "/api/debug/ydlp-version", "/history", "/schedule"]  # FIXED: TIKTOK
FACEBOOK_QUERY_PARAMS_TO_DROP = {"mibextid", "ref", "refsrc", "sfnsn", "__tn__"}  # CHANGED
DESKTOP_CHROME_120_UA = (  # CHANGED: shared desktop Chrome 120 user-agent.
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
TIKTOK_ANDROID_UA = "com.zhiliaoapp.musically/2022600030 (Linux; U; Android 7.1.2; GMT+01:00; Redmi Note 5 Pro Build/N2G48H; tt-ok/3.12.13.1)"  # FIXED: TIKTOK
TIKTOK_REFERER = "https://www.tiktok.com/"  # FIXED: TIKTOK
TIKTOK_FORMAT_SELECTOR = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"  # FIXED: TIKTOK
TIKTOK_EXTRACTOR_ARGS = {  # FIXED: TIKTOK
    "tiktok": {  # FIXED: TIKTOK
        "webpage_download": ["1"],  # FIXED: TIKTOK
        "api_hostname": ["api22-normal-c-useast2a.tiktokv.com"],  # FIXED: TIKTOK
    }  # FIXED: TIKTOK
}  # FIXED: TIKTOK
TIKTOK_METADATA_FALLBACK_FORMAT = {  # FIXED: TIKTOK
    "format_id": "best",  # FIXED: TIKTOK
    "ext": "mp4",  # FIXED: TIKTOK
    "quality": "Best Available",  # FIXED: TIKTOK
    "filesize": None,  # FIXED: TIKTOK
    "resolution": "Unknown",  # FIXED: TIKTOK
}  # FIXED: TIKTOK
INSTAGRAM_LINUX_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"  # CHANGED
FACEBOOK_EXTRACTOR_APP_ID = "2220391788200892"  # CHANGED
FACEBOOK_RETRY_ERROR_TOKENS = (  # CHANGED: Facebook retry on format/login style failures.
    "requested format is not available",
    "no video formats found",
    "format not available",
    "no suitable format",
    "login required",
    "login",
    "checkpoint",
    "cookie",
)
METADATA_TIMEOUT_SECONDS = 20  # CHANGED: metadata hard timeout requirement.
SCHEDULE_MAX_DAYS_AHEAD = 7  # NEW FEATURE: Download Scheduler
SCHEDULE_POLL_SECONDS = 60  # NEW FEATURE: Download Scheduler
SCHEDULED_JOBS_LOCK = threading.Lock()  # NEW FEATURE: Download Scheduler
SCHEDULER_THREAD_STARTED = False  # NEW FEATURE: Download Scheduler

TIMEZONE_ALIASES = {  # NEW FEATURE: Download Scheduler
    "UTC": "UTC",
    "GMT": "Etc/GMT",
    "WAT": "Africa/Lagos",
    "CAT": "Africa/Harare",
    "EAT": "Africa/Nairobi",
    "CET": "Europe/Berlin",
    "EST": "America/New_York",
    "PST": "America/Los_Angeles",
}

FALLBACK_TIMEZONE_OFFSETS = {
    "UTC": timezone.utc,
    "Etc/GMT": timezone.utc,
    "Africa/Lagos": timezone(timedelta(hours=1), name="Africa/Lagos"),
    "Africa/Harare": timezone(timedelta(hours=2), name="Africa/Harare"),
    "Africa/Nairobi": timezone(timedelta(hours=3), name="Africa/Nairobi"),
    "Europe/Berlin": timezone(timedelta(hours=1), name="Europe/Berlin"),
    "America/New_York": timezone(timedelta(hours=-5), name="America/New_York"),
    "America/Los_Angeles": timezone(timedelta(hours=-8), name="America/Los_Angeles"),
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
    format_id: str | None


@dataclass(frozen=True)
class MetadataRequest:
    url: str
    platform: str


@dataclass(frozen=True)
class ScheduleRequest:
    url: str
    platform: str
    format_id: str | None
    scheduled_at_utc: datetime
    timezone_label: str


def iso_utc(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)  # NEW FEATURE: Download Scheduler
    return current.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def get_scheduled_jobs_file_path() -> Path:
    configured_path = os.getenv("SCHEDULED_JOBS_FILE", "").strip()  # NEW FEATURE: Download Scheduler
    if configured_path:
        target = Path(configured_path)
    else:
        target = Path(__file__).resolve().parent / "scheduled_jobs.json"

    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        target.write_text("[]", encoding="utf-8")
    return target


def _load_scheduled_jobs_no_lock(path: Path) -> list[dict]:
    try:  # NEW FEATURE: Download Scheduler
        raw = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return []
    except OSError:
        return []

    if not raw:
        return []

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return []

    return payload if isinstance(payload, list) else []


def _save_scheduled_jobs_no_lock(path: Path, jobs: list[dict]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")  # NEW FEATURE: Download Scheduler
    temp_path.write_text(json.dumps(jobs, indent=2), encoding="utf-8")
    temp_path.replace(path)


def load_scheduled_jobs() -> list[dict]:
    path = get_scheduled_jobs_file_path()  # NEW FEATURE: Download Scheduler
    with SCHEDULED_JOBS_LOCK:
        return _load_scheduled_jobs_no_lock(path)


def save_scheduled_jobs(jobs: list[dict]) -> None:
    path = get_scheduled_jobs_file_path()  # NEW FEATURE: Download Scheduler
    with SCHEDULED_JOBS_LOCK:
        _save_scheduled_jobs_no_lock(path, jobs)


def resolve_timezone_name(timezone_label: str) -> tuple[str, timezone]:
    normalized = str(timezone_label or "").strip().upper()  # NEW FEATURE: Download Scheduler
    if not normalized:
        normalized = "UTC"

    timezone_name = TIMEZONE_ALIASES.get(normalized, normalized)
    fallback_timezone = FALLBACK_TIMEZONE_OFFSETS.get(timezone_name)

    if ZoneInfo is None:
        if fallback_timezone is not None:
            return timezone_name, fallback_timezone
        raise APIError("Timezone support is unavailable on this server.", 422)

    try:
        return timezone_name, ZoneInfo(timezone_name)
    except Exception as error:  # pragma: no cover - depends on runtime tzdata.
        if fallback_timezone is not None:
            logger.warning("tzdata unavailable for %s; using fixed-offset fallback.", timezone_name)
            return timezone_name, fallback_timezone
        raise APIError("Invalid timezone value.", 422) from error


def parse_schedule_datetime(scheduled_at_raw: str, timezone_label: str) -> tuple[datetime, str]:
    raw = str(scheduled_at_raw or "").strip()
    if not raw:
        raise APIError("scheduled_at is required and must be ISO8601.", 422)  # NEW FEATURE: Download Scheduler

    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as error:
        raise APIError("scheduled_at must be a valid ISO8601 datetime.", 422) from error

    timezone_name, tzinfo = resolve_timezone_name(timezone_label)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tzinfo)

    scheduled_utc = parsed.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)
    if scheduled_utc <= now_utc:
        raise APIError("scheduled_at must be in the future.", 422)
    if scheduled_utc > (now_utc + timedelta(days=SCHEDULE_MAX_DAYS_AHEAD)):
        raise APIError("scheduled_at cannot be more than 7 days ahead.", 422)

    return scheduled_utc, timezone_name


def parse_schedule_payload() -> ScheduleRequest:
    if not request.is_json:
        raise APIError("Request body must be JSON.", 400)  # NEW FEATURE: Download Scheduler

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise APIError("Malformed JSON payload.", 400)

    raw_url = str(data.get("url", "")).strip()
    normalized_url, detected_platform = validate_supported_url(raw_url)

    platform_hint = normalize_platform(data.get("platform"))
    if platform_hint and platform_hint != detected_platform:
        logger.info(
            "Schedule platform hint '%s' differs from detected '%s'. Using detected platform.",
            platform_hint,
            detected_platform,
        )

    raw_format_id = str(data.get("format_id", "")).strip()
    if raw_format_id and (len(raw_format_id) > 20 or not re.fullmatch(r"[A-Za-z0-9+-]+", raw_format_id)):
        raise APIError("format_id is invalid. Use only letters, numbers, +, -, max 20 chars.", 422)

    timezone_value = str(data.get("timezone", "UTC")).strip()
    scheduled_at_utc, timezone_name = parse_schedule_datetime(str(data.get("scheduled_at", "")), timezone_value)

    return ScheduleRequest(
        url=normalized_url,
        platform=detected_platform,
        format_id=raw_format_id or None,
        scheduled_at_utc=scheduled_at_utc,
        timezone_label=timezone_name,
    )


def update_scheduled_job(job_id: str, *, status: str, error_message: str | None = None) -> dict | None:
    path = get_scheduled_jobs_file_path()  # NEW FEATURE: Download Scheduler
    with SCHEDULED_JOBS_LOCK:
        jobs = _load_scheduled_jobs_no_lock(path)
        for job in jobs:
            if str(job.get("job_id")) != str(job_id):
                continue
            job["status"] = status
            job["error"] = error_message
            job["updated_at"] = iso_utc()
            _save_scheduled_jobs_no_lock(path, jobs)
            return job
    return None


def execute_scheduled_download(job: dict) -> None:
    work_dir: Path | None = None  # NEW FEATURE: Download Scheduler
    try:
        maybe_cleanup_stale_work_dirs()
        scheduled_job = DownloadRequest(
            url=str(job.get("url", "")),
            platform=str(job.get("platform", "")),
            download_type="video",
            quality="best",
            format_id=str(job.get("format_id", "")).strip() or None,
        )
        work_dir = create_work_dir()
        ydl_opts = build_ydl_options(scheduled_job, work_dir)

        if scheduled_job.platform == "facebook":
            try:
                extract_info_with_options(scheduled_job.url, ydl_opts, download=True)
            except DownloadError as first_error:
                if is_facebook_retryable_error(first_error):
                    retry_opts = dict(ydl_opts)
                    retry_opts["format"] = "worst"
                    extract_info_with_options(scheduled_job.url, retry_opts, download=True)
                else:
                    raise
        else:
            extract_info_with_options(scheduled_job.url, ydl_opts, download=True)

        pick_media_file(work_dir)
    finally:
        cleanup_work_dir(work_dir)


def process_due_scheduled_jobs_once() -> None:
    now_utc = datetime.now(timezone.utc)  # NEW FEATURE: Download Scheduler
    jobs = load_scheduled_jobs()
    due_job_ids: list[str] = []

    for job in jobs:
        if str(job.get("status")) != "scheduled":
            continue
        raw_scheduled_at = str(job.get("scheduled_at", ""))
        try:
            scheduled_at = datetime.fromisoformat(raw_scheduled_at.replace("Z", "+00:00"))
        except ValueError:
            update_scheduled_job(str(job.get("job_id", "")), status="failed", error_message="Invalid scheduled_at value.")
            continue

        if scheduled_at.astimezone(timezone.utc) <= now_utc:
            due_job_ids.append(str(job.get("job_id", "")))

    for job_id in due_job_ids:
        running_job = update_scheduled_job(job_id, status="running")
        if not running_job:
            continue

        try:
            execute_scheduled_download(running_job)
            update_scheduled_job(job_id, status="done")
        except Exception as error:
            mapped_error = map_download_error(  # FIXED: TIKTOK
                error,  # FIXED: TIKTOK
                platform=str(running_job.get("platform", "")),  # FIXED: TIKTOK
                url=str(running_job.get("url", "")),  # FIXED: TIKTOK
            )  # FIXED: TIKTOK
            update_scheduled_job(job_id, status="failed", error_message=mapped_error.message)


def scheduled_job_worker_loop() -> None:
    while True:  # NEW FEATURE: Download Scheduler
        try:
            process_due_scheduled_jobs_once()
        except Exception:
            logger.exception("Scheduled job worker iteration failed.")
        time.sleep(SCHEDULE_POLL_SECONDS)


def start_scheduled_job_worker() -> None:
    global SCHEDULER_THREAD_STARTED  # NEW FEATURE: Download Scheduler
    if SCHEDULER_THREAD_STARTED:
        return
    if os.getenv("DISABLE_SCHEDULER_THREAD", "0").strip() == "1":
        return

    worker = threading.Thread(
        target=scheduled_job_worker_loop,
        name="scheduled-download-worker",
        daemon=True,
    )
    worker.start()
    SCHEDULER_THREAD_STARTED = True


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

    if hostname == "fb.watch" or hostname.endswith(".fb.watch"):
        return raw_url  # CHANGED: fb.watch links are passed through untouched.

    if not (hostname == "facebook.com" or hostname.endswith(".facebook.com")):
        return raw_url

    path = parsed.path or "/"
    v_query = next(
        (
            value
            for key, value in parse_qsl(parsed.query, keep_blank_values=False)
            if key.strip().lower() == "v" and str(value).strip()
        ),
        "",
    )

    normalized_query = urlencode({"v": v_query}) if v_query else ""
    return urlunparse(("https", "www.facebook.com", path, "", normalized_query, ""))  # CHANGED


def normalize_tiktok_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)  # FIXED: TIKTOK
    hostname = (parsed.hostname or "").lower()  # FIXED: TIKTOK
    if not hostname:  # FIXED: TIKTOK
        return raw_url  # FIXED: TIKTOK

    if hostname == "vm.tiktok.com" or hostname.endswith(".vm.tiktok.com"):  # FIXED: TIKTOK
        return raw_url  # FIXED: TIKTOK

    if hostname == "m.tiktok.com" or hostname.endswith(".m.tiktok.com"):  # FIXED: TIKTOK
        path = parsed.path or "/"  # FIXED: TIKTOK
        mobile_video_match = re.search(r"/v/(\d+)\.html/?$", path, flags=re.IGNORECASE)  # FIXED: TIKTOK
        if mobile_video_match:  # FIXED: TIKTOK
            return urlunparse(("https", "www.tiktok.com", f"/video/{mobile_video_match.group(1)}", "", "", ""))  # FIXED: TIKTOK

        candidate_id = ""  # FIXED: TIKTOK
        direct_id_match = re.search(r"(\d{6,})", path)  # FIXED: TIKTOK
        if direct_id_match:  # FIXED: TIKTOK
            candidate_id = direct_id_match.group(1)  # FIXED: TIKTOK

        if not candidate_id:  # FIXED: TIKTOK
            for key, value in parse_qsl(parsed.query, keep_blank_values=False):  # FIXED: TIKTOK
                key_normalized = str(key).strip().lower()  # FIXED: TIKTOK
                value_normalized = str(value).strip()  # FIXED: TIKTOK
                if key_normalized in {"item_id", "video_id", "share_item_id", "id"} and value_normalized.isdigit():  # FIXED: TIKTOK
                    candidate_id = value_normalized  # FIXED: TIKTOK
                    break  # FIXED: TIKTOK

        if candidate_id:  # FIXED: TIKTOK
            return urlunparse(("https", "www.tiktok.com", f"/video/{candidate_id}", "", "", ""))  # FIXED: TIKTOK
        return raw_url  # FIXED: TIKTOK

    if hostname == "tiktok.com" or hostname == "www.tiktok.com":  # FIXED: TIKTOK
        direct_video_match = re.fullmatch(r"/@([^/]+)/video/(\d+)/?", parsed.path or "", flags=re.IGNORECASE)  # FIXED: TIKTOK
        if direct_video_match:  # FIXED: TIKTOK
            user_name = direct_video_match.group(1)  # FIXED: TIKTOK
            video_id = direct_video_match.group(2)  # FIXED: TIKTOK
            return urlunparse(("https", "www.tiktok.com", f"/@{user_name}/video/{video_id}", "", "", ""))  # FIXED: TIKTOK
        return raw_url  # FIXED: TIKTOK

    if hostname.endswith(".tiktok.com"):  # FIXED: TIKTOK
        return raw_url  # FIXED: TIKTOK

    return raw_url  # FIXED: TIKTOK


def normalize_instagram_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return raw_url

    if hostname == "instagr.am" or hostname.endswith(".instagr.am"):
        return raw_url  # CHANGED: instagr.am short links are kept untouched.

    if not (hostname == "instagram.com" or hostname.endswith(".instagram.com")):
        return raw_url

    path = parsed.path or "/"
    post_match = re.fullmatch(r"/p/([^/]+)/?", path, flags=re.IGNORECASE)
    reel_match = re.fullmatch(r"/reel/([^/]+)/?", path, flags=re.IGNORECASE)

    if post_match:
        clean_path = f"/p/{post_match.group(1)}/"
    elif reel_match:
        clean_path = f"/reel/{reel_match.group(1)}/"
    else:
        clean_path = path

    return urlunparse(("https", "www.instagram.com", clean_path, "", "", ""))  # CHANGED


def normalize_twitter_url(raw_url: str) -> str:
    parsed = urlparse(raw_url)
    hostname = (parsed.hostname or "").lower()
    if not hostname:
        return raw_url

    if hostname == "t.co" or hostname.endswith(".t.co"):
        return raw_url  # CHANGED: t.co links are kept for yt-dlp resolution.

    if hostname in {"twitter.com", "www.twitter.com"} or hostname.endswith(".twitter.com"):
        normalized_host = "x.com"
    elif hostname in {"x.com", "www.x.com"} or hostname.endswith(".x.com"):
        normalized_host = "x.com"
    else:
        return raw_url

    clean_path = parsed.path or "/"
    return urlunparse(("https", normalized_host, clean_path, "", "", ""))  # CHANGED


def normalize_platform_url(raw_url: str, platform: str) -> str:
    if platform == "facebook":
        return normalize_facebook_url(raw_url)  # CHANGED
    if platform == "tiktok":
        return normalize_tiktok_url(raw_url)  # CHANGED
    if platform == "instagram":
        return normalize_instagram_url(raw_url)  # CHANGED
    if platform == "twitter":
        return normalize_twitter_url(raw_url)  # CHANGED
    return raw_url


def is_facebook_retryable_error(error: Exception) -> bool:
    message = str(error).lower()
    return any(token in message for token in FACEBOOK_RETRY_ERROR_TOKENS)  # CHANGED


def extract_info_with_options(video_url: str, ydl_opts: dict, *, download: bool):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(video_url, download=download)  # CHANGED


def extract_info_with_timeout(video_url: str, ydl_opts: dict, *, download: bool, timeout_seconds: int):
    with ThreadPoolExecutor(max_workers=1) as executor:  # CHANGED: metadata timeout enforcement.
        future = executor.submit(extract_info_with_options, video_url, ydl_opts, download=download)
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError as error:
            future.cancel()
            raise APIError("Metadata request timed out. Please try again.", 504) from error


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

    normalized_url = normalize_platform_url(raw_url, detected_platform)  # CHANGED

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


def get_platform_ydl_opts(platform: str, format_id: str | None = None) -> dict:
    """Return robust yt-dlp base options per platform."""  # CHANGED
    normalized_platform = normalize_platform(platform)
    selected_format = (format_id or "").strip()

    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "retries": 5,
        "fragment_retries": 5,
        "skip_unavailable_fragments": True,
        "ignoreerrors": False,
        "nocheckcertificate": False,
        "noplaylist": True,
        "geo_bypass": True,
    }

    browser = os.getenv("YTDLP_COOKIES_BROWSER", "").strip().lower()
    cookie_file = os.getenv("YTDLP_COOKIES_FILE", "").strip()

    if normalized_platform == "youtube":
        opts["format"] = selected_format or "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        existing_extractors = opts.get("extractor_args", {})
        youtube_args = dict(existing_extractors.get("youtube", {}))
        if "player_client" not in youtube_args:
            youtube_args["player_client"] = ["android", "web"]
        if "skip" not in youtube_args:
            youtube_args["skip"] = ["dash", "hls"]
        existing_extractors["youtube"] = youtube_args
        opts["extractor_args"] = existing_extractors
        opts["merge_output_format"] = "mp4"
        existing_headers = dict(opts.get("http_headers", {}))
        if "User-Agent" not in existing_headers:
            existing_headers["User-Agent"] = DESKTOP_CHROME_120_UA
        if "Accept-Language" not in existing_headers:
            existing_headers["Accept-Language"] = "en-US,en;q=0.9"
        opts["http_headers"] = existing_headers
    elif normalized_platform == "facebook":
        opts["format"] = "best"  # CHANGED: Facebook ignores format_id by requirement.
        opts["http_headers"] = {"User-Agent": DESKTOP_CHROME_120_UA}
        opts["extractor_args"] = {"facebook": {"app_id": FACEBOOK_EXTRACTOR_APP_ID}}
        opts["socket_timeout"] = 30
        if browser:
            opts["cookiesfrombrowser"] = (browser,)
    elif normalized_platform == "tiktok":
        tiktok_opts = {
            "quiet": True,
            "no_warnings": True,
            "socket_timeout": 30,
            "retries": 10,
            "fragment_retries": 10,
            "skip_unavailable_fragments": True,
            "format": TIKTOK_FORMAT_SELECTOR,
            "merge_output_format": "mp4",
            "http_headers": {
                "User-Agent": TIKTOK_ANDROID_UA,
                "Referer": TIKTOK_REFERER,
            },
            "extractor_args": TIKTOK_EXTRACTOR_ARGS,
            "cookiefile": None,
        }
        tiktok_headers = dict(tiktok_opts.get("http_headers", {}))
        if "Referer" not in tiktok_headers:
            tiktok_headers["Referer"] = TIKTOK_REFERER
        if "Origin" not in tiktok_headers:
            tiktok_headers["Origin"] = "https://www.tiktok.com"
        tiktok_opts["http_headers"] = tiktok_headers
        return tiktok_opts
    elif normalized_platform == "instagram":
        opts["format"] = selected_format or "best"
        opts["http_headers"] = {"User-Agent": INSTAGRAM_LINUX_UA}
        opts["socket_timeout"] = 25
        if browser:
            opts["cookiesfrombrowser"] = (browser,)
    elif normalized_platform == "twitter":
        if selected_format:
            opts["format"] = selected_format
        if "format" not in opts:
            opts["format"] = "best[ext=mp4]/best"
        opts["http_headers"] = {"User-Agent": DESKTOP_CHROME_120_UA}
        opts["extractor_args"] = {"twitter": {"api": "graphql"}}
        opts["socket_timeout"] = 20
    else:
        opts["format"] = selected_format or "best"
        opts["http_headers"] = {"User-Agent": DESKTOP_CHROME_120_UA}
        opts["socket_timeout"] = 30

    if cookie_file:
        opts["cookiefile"] = cookie_file

    if _env_bool("YTDLP_FORCE_IPV4", True):
        opts["source_address"] = "0.0.0.0"  # CHANGED

    return opts


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


def select_thumbnail_url(info: dict) -> str | None:
    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list):  # CHANGED: prefer first available thumbnail URL.
        for item in thumbnails:
            if isinstance(item, dict):
                thumb_url = str(item.get("url", "")).strip()
                if thumb_url:
                    return thumb_url

    thumbnail = str(info.get("thumbnail") or "").strip()
    return thumbnail or None


def format_duration_string(duration_seconds: int | None) -> str | None:
    if duration_seconds is None or duration_seconds < 0:
        return None  # CHANGED

    minutes, seconds = divmod(duration_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def build_metadata_formats(info: dict) -> list[dict]:
    raw_formats = info.get("formats")
    if not isinstance(raw_formats, list) or not raw_formats:
        return [  # CHANGED: fallback metadata option when source has no formats array.
            {
                "format_id": "best",
                "ext": "auto",
                "height": None,
                "filesize": None,
                "label": "Best available",
            }
        ]

    collected: list[dict] = []
    seen: set[str] = set()

    for current in raw_formats:
        if not isinstance(current, dict):
            continue

        format_id = str(current.get("format_id", "")).strip()
        if not format_id or format_id in seen:
            continue

        ext = str(current.get("ext", "")).strip().lower()
        if not ext:
            ext = "auto"  # CHANGED

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
    if not collected:
        return [  # CHANGED: fallback if parsing yields no compatible variants.
            {
                "format_id": "best",
                "ext": "auto",
                "height": None,
                "filesize": None,
                "label": "Best available",
            }
        ]

    collected = collected[:9]  # CHANGED: cap quality variants before adding audio option.
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
    return collected[:10]  # CHANGED: metadata option cap.


def build_ydl_options(job: DownloadRequest, work_dir: Path, force_format: str | None = None) -> dict:
    is_audio_request = job.download_type == "audio" or job.format_id == "audio-only"
    selected_format = force_format  # CHANGED
    if not selected_format and job.platform != "facebook" and job.format_id and job.format_id != "audio-only":
        selected_format = job.format_id  # CHANGED

    opts = get_platform_ydl_opts(job.platform, selected_format)  # CHANGED
    opts["outtmpl"] = str(work_dir / "%(id)s.%(ext)s")
    base_defaults = {
        "concurrent_fragment_downloads": 4,
        "retries": 5,
        "fragment_retries": 5,
        "socket_timeout": 15,
        "noprogress": True,
        "quiet": True,
    }
    for key, value in base_defaults.items():
        if key not in opts:
            opts[key] = value

    if "postprocessor_args" not in opts:
        opts["postprocessor_args"] = {"ffmpeg": ["-movflags", "+faststart"]}

    if job.platform == "youtube" and not job.format_id:
        opts["format"] = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"

    if job.platform == "youtube" and "/shorts/" in job.url:
        extractor_args = dict(opts.get("extractor_args", {}))
        youtube_args = dict(extractor_args.get("youtube", {}))
        youtube_args["player_client"] = ["web"]
        extractor_args["youtube"] = youtube_args
        opts["extractor_args"] = extractor_args

    if is_audio_request and job.platform not in {"facebook", "tiktok"}:  # FIXED: TIKTOK
        opts["format"] = "bestaudio/best"  # CHANGED
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]

    return opts


def build_metadata_ydl_options(job: MetadataRequest) -> dict:
    opts = get_platform_ydl_opts(job.platform)  # CHANGED
    opts["skip_download"] = True
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


def map_download_error(error: Exception, platform: str | None = None, url: str | None = None) -> APIError:  # FIXED: TIKTOK
    raw_message = str(error)  # FIXED: TIKTOK
    message = raw_message.lower()  # FIXED: TIKTOK
    url_value = str(url or "").lower()  # FIXED: TIKTOK
    tiktok_context = platform == "tiktok" or "tiktok" in url_value  # FIXED: TIKTOK

    if tiktok_context and "not available" in message:  # FIXED: TIKTOK
        return APIError("This TikTok video is unavailable or has been deleted.", 410)  # FIXED: TIKTOK
    if tiktok_context and ("unable to find video" in message or "no video formats" in message):  # FIXED: TIKTOK
        return APIError("TikTok video not found. Check the link and try again.", 404)  # FIXED: TIKTOK
    if tiktok_context and ("age" in message or "mature" in message):  # FIXED: TIKTOK
        return APIError("This TikTok is age-restricted and cannot be downloaded.", 403)  # FIXED: TIKTOK
    if tiktok_context and "private" in message:  # FIXED: TIKTOK
        return APIError("This TikTok account is private.", 403)  # FIXED: TIKTOK
    if tiktok_context and ("429" in message or "rate" in message):  # FIXED: TIKTOK
        return APIError("TikTok is rate limiting downloads. Wait 1-2 minutes and retry.", 429)  # FIXED: TIKTOK
    if tiktok_context and ("unable to extract" in message or "unsupported url" in message):  # FIXED: TIKTOK
        return APIError(  # FIXED: TIKTOK
            "Could not read this TikTok URL. Try copying the link directly from the TikTok app or website.",  # FIXED: TIKTOK
            422,  # FIXED: TIKTOK
        )  # FIXED: TIKTOK
    if tiktok_context:  # FIXED: TIKTOK
        return APIError("TikTok download failed. Try a different video or wait a moment.", 500)  # FIXED: TIKTOK

    if platform == "facebook" and ("login required" in message or "checkpoint" in message):
        return APIError("This Facebook video requires login. Only public videos are supported.", 403)  # CHANGED
    if platform == "instagram" and "login_required" in message:
        return APIError("This Instagram content requires login. Only public posts are supported.", 403)  # CHANGED
    if platform == "twitter" and "could not find tweet" in message:
        return APIError("This tweet/post could not be found. It may be deleted or private.", 404)  # CHANGED
    if "unsupported url" in message or "no suitable extractor" in message:
        return APIError("This URL is not supported. Paste a direct video link.", 422)  # CHANGED
    if "429" in message or "too many requests" in message or "rate limit" in message:
        return APIError("The platform is rate limiting us. Please wait 1-2 minutes and try again.", 429)  # CHANGED
    if "ssl" in message or "certificate" in message or "tls" in message:
        return APIError("Secure connection to the platform failed. Please try again.", 502)  # CHANGED
    if "private" in message or "sign in" in message or "login" in message:
        return APIError("This video is private or requires login.", 403)
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


def ensure_runtime_services_started() -> None:
    global RUNTIME_STARTED
    if RUNTIME_STARTED:
        return
    if app.testing or app.config.get("TESTING"):
        return

    with RUNTIME_START_LOCK:
        if RUNTIME_STARTED:
            return
        try:
            maybe_cleanup_stale_work_dirs()
        except Exception:
            logger.exception("Startup workspace cleanup failed.")

        start_scheduled_job_worker()
        RUNTIME_STARTED = True


@app.before_request
def before_request_start_services():
    ensure_runtime_services_started()


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/download")
def download_page():
    return render_template("download.html")


@app.route("/history")
def history_page():
    return render_template("history.html")


@app.route("/schedule")
def schedule_page():
    return render_template("schedule.html")  # NEW FEATURE: Download Scheduler


@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify(
        {
            "status": "ok",
            "endpoints": ENDPOINTS_LIST,
            "supported_platforms": sorted(SUPPORTED_PLATFORM_DOMAINS.keys()),
        }
    )


@app.route("/api/debug/ydlp-version", methods=["GET"])  # FIXED: TIKTOK
def ydlp_version_debug():  # FIXED: TIKTOK
    return jsonify({"yt_dlp_version": get_yt_dlp_version(), "tiktok_status": "ok"})  # FIXED: TIKTOK


@app.route("/api/schedule", methods=["POST"])
def schedule_download():
    try:
        payload = parse_schedule_payload()  # NEW FEATURE: Download Scheduler
        now_iso = iso_utc()
        new_job = {
            "job_id": str(uuid.uuid4()),
            "url": payload.url,
            "platform": payload.platform,
            "format_id": payload.format_id,
            "scheduled_at": iso_utc(payload.scheduled_at_utc),
            "timezone": payload.timezone_label,
            "status": "scheduled",
            "error": None,
            "created_at": now_iso,
            "updated_at": now_iso,
        }

        jobs = load_scheduled_jobs()
        jobs.append(new_job)
        save_scheduled_jobs(jobs)

        return jsonify(
            {
                "job_id": new_job["job_id"],
                "scheduled_at": new_job["scheduled_at"],
                "status": new_job["status"],
            }
        )
    except APIError as error:
        return jsonify({"error": error.message}), error.status_code


@app.route("/api/schedule", methods=["GET"])
def list_scheduled_downloads():
    jobs = load_scheduled_jobs()  # NEW FEATURE: Download Scheduler
    pending_jobs = [
        {
            "job_id": str(job.get("job_id", "")),
            "url": str(job.get("url", "")),
            "platform": str(job.get("platform", "")),
            "format_id": job.get("format_id"),
            "scheduled_at": str(job.get("scheduled_at", "")),
            "timezone": str(job.get("timezone", "UTC")),
            "status": str(job.get("status", "scheduled")),
        }
        for job in jobs
        if str(job.get("status")) == "scheduled"
    ]
    pending_jobs.sort(key=lambda item: item.get("scheduled_at", ""))
    return jsonify({"jobs": pending_jobs})


@app.route("/api/schedule/<job_id>", methods=["DELETE"])
def cancel_scheduled_download(job_id: str):
    current = update_scheduled_job(job_id, status="cancelled")  # NEW FEATURE: Download Scheduler
    if not current:
        return jsonify({"error": "Scheduled job not found."}), 404
    return jsonify({"job_id": job_id, "status": "cancelled"})


@app.route("/api/metadata", methods=["POST"])
@limiter.limit(DOWNLOAD_RATE_LIMIT)
def video_metadata():
    try:
        job = parse_metadata_payload()
        if job.platform == "tiktok":  # FIXED: TIKTOK
            job = MetadataRequest(url=normalize_tiktok_url(job.url), platform=job.platform)  # FIXED: TIKTOK
        ydl_opts = build_metadata_ydl_options(job)

        try:
            info = extract_info_with_timeout(
                job.url,
                ydl_opts,
                download=False,
                timeout_seconds=METADATA_TIMEOUT_SECONDS,
            )  # CHANGED
        except DownloadError as first_error:
            if job.platform == "facebook" and is_facebook_retryable_error(first_error):
                retry_opts = dict(ydl_opts)
                retry_opts["format"] = "worst"  # CHANGED: silent fallback retry for Facebook metadata failures.
                info = extract_info_with_timeout(
                    job.url,
                    retry_opts,
                    download=False,
                    timeout_seconds=METADATA_TIMEOUT_SECONDS,
                )
            else:
                raise

        info = normalize_info_payload(info)
        if not isinstance(info, dict):
            raise APIError("Unable to inspect this link for metadata.", 422)

        raw_formats = info.get("formats") if isinstance(info, dict) else None  # FIXED: TIKTOK
        if job.platform == "tiktok" and (not isinstance(raw_formats, list) or not raw_formats):  # FIXED: TIKTOK
            formats = [dict(TIKTOK_METADATA_FALLBACK_FORMAT)]  # FIXED: TIKTOK
        else:  # FIXED: TIKTOK
            formats = build_metadata_formats(info)  # FIXED: TIKTOK

        duration_value = info.get("duration")
        try:
            duration = int(duration_value) if duration_value is not None else None
        except (TypeError, ValueError):
            duration = None

        return jsonify(
            {
                "title": str(info.get("title", "Untitled video")),
                "thumbnail": select_thumbnail_url(info),  # CHANGED
                "duration": duration,
                "duration_string": format_duration_string(duration),  # CHANGED
                "uploader": str(info.get("uploader") or info.get("channel") or "Unknown uploader"),
                "platform": job.platform,
                "formats": formats,
            }
        )

    except APIError as error:
        return jsonify({"error": error.message}), error.status_code
    except DownloadError as error:
        mapped_error = map_download_error(  # FIXED: TIKTOK
            error,  # FIXED: TIKTOK
            platform=job.platform if "job" in locals() else None,  # FIXED: TIKTOK
            url=job.url if "job" in locals() else None,  # FIXED: TIKTOK
        )  # FIXED: TIKTOK
        return jsonify({"error": mapped_error.message}), mapped_error.status_code
    except Exception as error:
        logger.exception("Unhandled metadata error")
        mapped_error = map_download_error(  # FIXED: TIKTOK
            error,  # FIXED: TIKTOK
            platform=job.platform if "job" in locals() else None,  # FIXED: TIKTOK
            url=job.url if "job" in locals() else None,  # FIXED: TIKTOK
        )  # FIXED: TIKTOK
        return jsonify({"error": mapped_error.message}), mapped_error.status_code


@app.route("/api/download", methods=["POST"])
@limiter.limit(DOWNLOAD_RATE_LIMIT, deduct_when=should_count_successful_download)
def download_video():
    work_dir: Path | None = None

    try:
        maybe_cleanup_stale_work_dirs()
        job = parse_download_payload()
        if job.platform == "tiktok":  # FIXED: TIKTOK
            job = DownloadRequest(  # FIXED: TIKTOK
                url=normalize_tiktok_url(job.url),  # FIXED: TIKTOK
                platform=job.platform,  # FIXED: TIKTOK
                download_type=job.download_type,  # FIXED: TIKTOK
                quality=job.quality,  # FIXED: TIKTOK
                format_id=job.format_id,  # FIXED: TIKTOK
            )  # FIXED: TIKTOK
        work_dir = create_work_dir()
        ydl_opts = build_ydl_options(job, work_dir)

        logger.info(
            "Starting download platform=%s type=%s quality=%s format_id=%s",
            job.platform,
            job.download_type,
            job.quality,
            job.format_id,
        )

        if job.platform == "facebook":
            try:
                info = extract_info_with_options(job.url, ydl_opts, download=True)
            except DownloadError as first_error:
                if is_facebook_retryable_error(first_error):
                    retry_opts = dict(ydl_opts)
                    retry_opts["format"] = "worst"  # CHANGED: required silent Facebook retry fallback.
                    info = extract_info_with_options(job.url, retry_opts, download=True)
                else:
                    raise
        else:
            info = extract_info_with_options(job.url, ydl_opts, download=True)

        info = normalize_info_payload(info)
        media_path = pick_media_file(work_dir)
        title = sanitize_filename(str(info.get("title", "video"))) if isinstance(info, dict) else "video"
        download_name = f"{title}{media_path.suffix.lower()}"
        file_size = os.path.getsize(media_path)

        def stream_file(filepath: Path, chunk_size: int = 262144):
            with open(filepath, "rb") as file_handle:
                while True:
                    chunk = file_handle.read(chunk_size)
                    if not chunk:
                        break
                    yield chunk

        stream_iterable = stream_file(media_path)
        # PERF: avoid stream_with_context in tests to prevent request context teardown issues.
        if not (app.testing or app.config.get("TESTING")):
            stream_iterable = stream_with_context(stream_iterable)

        response = Response(
            stream_iterable,
            status=200,
            headers={
                "Content-Disposition": f'attachment; filename="{download_name}"',
                "Content-Type": "video/mp4",
                "Content-Length": str(file_size),
                "Accept-Ranges": "bytes",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )
        response.call_on_close(lambda: cleanup_work_dir(work_dir))
        return response

    except APIError as error:
        cleanup_work_dir(work_dir)
        return jsonify({"error": error.message}), error.status_code
    except DownloadError as error:
        cleanup_work_dir(work_dir)
        mapped_error = map_download_error(  # FIXED: TIKTOK
            error,  # FIXED: TIKTOK
            platform=job.platform if "job" in locals() else None,  # FIXED: TIKTOK
            url=job.url if "job" in locals() else None,  # FIXED: TIKTOK
        )  # FIXED: TIKTOK
        logger.warning("DownloadError: %s", error)
        return jsonify({"error": mapped_error.message}), mapped_error.status_code
    except Exception as error:
        cleanup_work_dir(work_dir)
        logger.exception("Unhandled download error")
        mapped_error = map_download_error(  # FIXED: TIKTOK
            error,  # FIXED: TIKTOK
            platform=job.platform if "job" in locals() else None,  # FIXED: TIKTOK
            url=job.url if "job" in locals() else None,  # FIXED: TIKTOK
        )  # FIXED: TIKTOK
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


@app.route("/robots.txt", methods=["GET"])
def robots_txt():
    body = "User-agent: *\nAllow: /\n"  # NEW FEATURE: robots/sitemap
    return Response(body, mimetype="text/plain")


@app.route("/sitemap.xml", methods=["GET"])
def sitemap_xml():
    base_url = request.url_root.rstrip("/")  # NEW FEATURE: robots/sitemap
    urls = ["/", "/history", "/schedule"]
    url_entries = "".join(f"<url><loc>{base_url}{path}</loc></url>" for path in urls)
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{url_entries}"
        "</urlset>"
    )
    return Response(body, mimetype="application/xml")


if __name__ == "__main__":
    port = _env_int("PORT", 5000)
    debug = os.getenv("FLASK_DEBUG", "0").strip() == "1"
    ensure_runtime_services_started()
    print("\n" + "=" * 50)
    print("Multi-Platform Video Downloader Starting")
    print("=" * 50)
    print(f"URL: http://127.0.0.1:{port}")
    print(f"Alternative: http://localhost:{port}")
    print("Supported platforms: YouTube, Facebook, TikTok, Twitter/X, Instagram")
    print("=" * 50 + "\n")
    app.run(debug=debug, threaded=True, host="0.0.0.0", port=port)
