import unittest
import uuid
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import vid


class FakeYoutubeDL:
    last_options = None

    def __init__(self, options):
        self.options = options
        FakeYoutubeDL.last_options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, _url, download=True):
        if not download:
            return {
                "id": "unit_test_id",
                "title": "My Test Video",
                "thumbnail": "https://example.com/thumb.jpg",
                "duration": 187,
                "uploader": "Uploader Unit",
                "formats": [
                    {
                        "format_id": "22",
                        "ext": "mp4",
                        "height": 720,
                        "vcodec": "avc1.64001F",
                        "acodec": "mp4a.40.2",
                        "filesize": 7_000_000,
                    },
                    {
                        "format_id": "18",
                        "ext": "mp4",
                        "height": 360,
                        "vcodec": "avc1.42001E",
                        "acodec": "none",
                        "filesize": 2_500_000,
                    },
                    {
                        "format_id": "171",
                        "ext": "webm",
                        "height": None,
                        "vcodec": "none",
                        "acodec": "vorbis",
                        "filesize": 1_100_000,
                    },
                    {
                        "format_id": "badext",
                        "ext": "flv",
                        "height": 720,
                        "vcodec": "avc1",
                        "acodec": "mp4a",
                        "filesize": 4_000_000,
                    },
                ],
            }

        output_template = self.options["outtmpl"]
        ext = "mp3" if self.options.get("postprocessors") else "mp4"
        file_path = Path(output_template.replace("%(id)s", "unit_test_id").replace("%(ext)s", ext))
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"fake-video-content")
        return {
            "id": "unit_test_id",
            "title": "My Test Video",
            "ext": ext,
        }


class FakeNetworkErrorYoutubeDL:
    def __init__(self, _options):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, _url, download=True):
        raise vid.DownloadError("Unable to download webpage: [WinError 10013] socket blocked")


class FakeFacebookFormatRetryYoutubeDL:
    attempted_formats = []

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, _url, download=True):
        current_format = self.options.get("format")
        FakeFacebookFormatRetryYoutubeDL.attempted_formats.append(current_format)

        if current_format == "best":
            raise vid.DownloadError("Requested format is not available")

        if download:
            output_template = self.options["outtmpl"]
            file_path = Path(output_template.replace("%(id)s", "fb_retry_id").replace("%(ext)s", "mp4"))
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(b"facebook-retry-content")

        return {
            "id": "fb_retry_id",
            "title": "Facebook Retry Video",
            "ext": "mp4",
            "formats": [],
        }


class FakeFacebookReelFallbackYoutubeDL:
    attempted_urls = []

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=True):
        FakeFacebookReelFallbackYoutubeDL.attempted_urls.append(url)
        if "/reel/" in url:
            raise vid.DownloadError("Could not download video data")

        if download:
            output_template = self.options["outtmpl"]
            file_path = Path(output_template.replace("%(id)s", "fb_embed_id").replace("%(ext)s", "mp4"))
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_bytes(b"facebook-embed-content")

        return {
            "id": "fb_embed_id",
            "title": "Facebook Embed Video",
            "thumbnail": "https://example.com/fb-thumb.jpg",
            "duration": 62,
            "uploader": "Facebook Unit",
            "formats": [
                {
                    "format_id": "fb18",
                    "ext": "mp4",
                    "height": 360,
                    "vcodec": "avc1.42001E",
                    "acodec": "mp4a.40.2",
                    "filesize": 3_500_000,
                }
            ],
        }


class FakeFacebookMetadataRetryYoutubeDL:
    attempted_formats = []

    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, _url, download=True):
        current_format = self.options.get("format")
        FakeFacebookMetadataRetryYoutubeDL.attempted_formats.append(current_format)
        if current_format == "best":
            raise vid.DownloadError("Requested format is not available")
        return {
            "id": "fb_retry_meta",
            "title": "Facebook Meta Retry",
            "thumbnail": "https://example.com/fb-meta.jpg",
            "duration": 95,
            "uploader": "FB Meta",
            "formats": [
                {
                    "format_id": "18",
                    "ext": "mp4",
                    "height": 360,
                    "vcodec": "avc1.42001E",
                    "acodec": "mp4a.40.2",
                    "filesize": 2_000_000,
                }
            ],
        }


class FakeTikTokMetadataNoFormatsYoutubeDL:
    def __init__(self, _options):
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, _url, download=True):
        if download:
            raise AssertionError("metadata fake should be called with download=False")
        return {
            "id": "tt_no_formats",
            "title": "TikTok Hidden Formats",
            "thumbnail": "https://example.com/tiktok.jpg",
            "duration": 25,
            "uploader": "tt-user",
        }


class VideoDownloaderApiTests(unittest.TestCase):
    def setUp(self):
        vid.app.config["TESTING"] = True
        self.client = vid.app.test_client()
        self.client_ip = f"test-client-{uuid.uuid4().hex}"
        self.headers = {"X-Forwarded-For": self.client_ip}
        self.schedule_temp_dir = Path("tests/.tmp_schedule") / uuid.uuid4().hex
        self.schedule_temp_dir.mkdir(parents=True, exist_ok=True)
        self.schedule_jobs_file = self.schedule_temp_dir / "scheduled_jobs.json"
        self.schedule_env_patch = patch.dict(
            "os.environ",
            {
                "SCHEDULED_JOBS_FILE": str(self.schedule_jobs_file),
                "DISABLE_SCHEDULER_THREAD": "1",
            },
            clear=False,
        )
        self.schedule_env_patch.start()

    def tearDown(self):
        self.schedule_env_patch.stop()
        shutil.rmtree(self.schedule_temp_dir, ignore_errors=True)
        if hasattr(vid.limiter, "reset"):
            vid.limiter.reset()

    @staticmethod
    def _iso_utc_in(minutes: int = 10, *, days: int = 0) -> str:
        value = datetime.now(timezone.utc) + timedelta(days=days, minutes=minutes)
        return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")

    def test_health_endpoint(self):
        response = self.client.get("/api/health")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("/api/download", payload["endpoints"])
        self.assertIn("/api/metadata", payload["endpoints"])
        self.assertIn("/history", payload["endpoints"])

    def test_debug_ydlp_version_endpoint(self):
        response = self.client.get("/api/debug/ydlp-version")
        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertIn("yt_dlp_version", payload)
        self.assertEqual(payload["tiktok_status"], "ok")

    def test_schedule_create_list_cancel_flow(self):
        response = self.client.post(
            "/api/schedule",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "platform": "youtube",
                "format_id": "22",
                "scheduled_at": self._iso_utc_in(15),
                "timezone": "UTC",
            },
            headers=self.headers,
        )

        created_payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(created_payload["status"], "scheduled")
        self.assertTrue(created_payload["job_id"])

        list_response = self.client.get("/api/schedule")
        list_payload = list_response.get_json()
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(len(list_payload["jobs"]), 1)
        self.assertEqual(list_payload["jobs"][0]["job_id"], created_payload["job_id"])
        self.assertEqual(list_payload["jobs"][0]["platform"], "youtube")

        cancel_response = self.client.delete(f"/api/schedule/{created_payload['job_id']}")
        cancel_payload = cancel_response.get_json()
        self.assertEqual(cancel_response.status_code, 200)
        self.assertEqual(cancel_payload["status"], "cancelled")

        list_after_cancel = self.client.get("/api/schedule")
        list_after_cancel_payload = list_after_cancel.get_json()
        self.assertEqual(list_after_cancel.status_code, 200)
        self.assertEqual(list_after_cancel_payload["jobs"], [])

    def test_schedule_rejects_past_datetime(self):
        past_value = (datetime.now(timezone.utc) - timedelta(minutes=2)).replace(microsecond=0).isoformat()
        response = self.client.post(
            "/api/schedule",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "scheduled_at": past_value,
                "timezone": "UTC",
            },
            headers=self.headers,
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 422)
        self.assertIn("future", payload["error"].lower())

    def test_schedule_rejects_datetime_beyond_seven_days(self):
        response = self.client.post(
            "/api/schedule",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "scheduled_at": self._iso_utc_in(minutes=0, days=8),
                "timezone": "UTC",
            },
            headers=self.headers,
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 422)
        self.assertIn("7 days", payload["error"])

    def test_schedule_rejects_invalid_timezone(self):
        response = self.client.post(
            "/api/schedule",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "scheduled_at": self._iso_utc_in(20),
                "timezone": "NOT_A_TIMEZONE",
            },
            headers=self.headers,
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 422)
        self.assertIn("timezone", payload["error"].lower())

    def test_schedule_uses_detected_platform_when_hint_differs(self):
        create_response = self.client.post(
            "/api/schedule",
            json={
                "url": "https://www.youtube.com/watch?v=abc123",
                "platform": "facebook",
                "scheduled_at": self._iso_utc_in(30),
                "timezone": "UTC",
            },
            headers=self.headers,
        )
        self.assertEqual(create_response.status_code, 200)

        list_response = self.client.get("/api/schedule")
        list_payload = list_response.get_json()
        self.assertEqual(list_response.status_code, 200)
        self.assertEqual(list_payload["jobs"][0]["platform"], "youtube")

    def test_rejects_non_json_payload(self):
        response = self.client.post("/api/download", data="url=https://youtube.com/watch?v=abc")
        payload = response.get_json()

        self.assertEqual(response.status_code, 400)
        self.assertIn("json", payload["error"].lower())

    def test_rejects_private_network_url(self):
        response = self.client.post(
            "/api/download",
            json={"url": "http://127.0.0.1/private", "platform": "YouTube"},
            headers=self.headers,
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 400)
        self.assertIn("local", payload["error"].lower())

    def test_rejects_unsupported_platform_domain(self):
        response = self.client.post(
            "/api/download",
            json={"url": "https://example.com/video", "platform": "YouTube"},
            headers=self.headers,
        )
        payload = response.get_json()

        self.assertEqual(response.status_code, 400)
        self.assertIn("unsupported platform", payload["error"].lower())

    def test_validate_supported_url_normalizes_facebook_videos_path_and_tracking_params(self):
        url, platform = vid.validate_supported_url(
            "https://m.facebook.com/some.page/videos/744372871141776/?mibextid=abcd1234&ref=share"
        )

        self.assertEqual(platform, "facebook")
        self.assertEqual(url, "https://www.facebook.com/some.page/videos/744372871141776/")

    def test_validate_supported_url_keeps_fb_watch_url_unchanged(self):
        original = "https://fb.watch/abc123/?mibextid=tracking-value"
        url, platform = vid.validate_supported_url(original)
        self.assertEqual(platform, "facebook")
        self.assertEqual(url, original)

    def test_validate_supported_url_normalizes_tiktok_direct_links(self):
        url, platform = vid.validate_supported_url("https://tiktok.com/@sample/video/1234567890?is_from_webapp=1")
        self.assertEqual(platform, "tiktok")
        self.assertEqual(url, "https://www.tiktok.com/@sample/video/1234567890")

    def test_validate_supported_url_keeps_tiktok_vm_short_links(self):
        original = "https://vm.tiktok.com/ZM1234567/"
        url, platform = vid.validate_supported_url(original)
        self.assertEqual(platform, "tiktok")
        self.assertEqual(url, original)

    def test_metadata_returns_expected_fields(self):
        with patch("vid.yt_dlp.YoutubeDL", FakeYoutubeDL):
            response = self.client.post(
                "/api/metadata",
                json={"url": "https://www.youtube.com/watch?v=abc123"},
                headers=self.headers,
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["title"], "My Test Video")
        self.assertEqual(payload["thumbnail"], "https://example.com/thumb.jpg")
        self.assertIn("formats", payload)
        self.assertTrue(any(item["format_id"] == "audio-only" for item in payload["formats"]))
        self.assertTrue(any(item["format_id"] == "22" for item in payload["formats"]))

    def test_metadata_tiktok_uses_synthetic_format_when_formats_missing(self):
        with patch("vid.yt_dlp.YoutubeDL", FakeTikTokMetadataNoFormatsYoutubeDL):
            response = self.client.post(
                "/api/metadata",
                json={"url": "https://www.tiktok.com/@sample/video/1234567890"},
                headers=self.headers,
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["platform"], "tiktok")
        self.assertEqual(payload["formats"], [vid.TIKTOK_METADATA_FALLBACK_FORMAT])

    def test_metadata_rejects_private_ip(self):
        response = self.client.post(
            "/api/metadata",
            json={"url": "https://127.0.0.1/video"},
            headers=self.headers,
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 400)
        self.assertIn("local", payload["error"].lower())

    def test_metadata_rejects_unsupported_domain(self):
        response = self.client.post(
            "/api/metadata",
            json={"url": "https://example.com/video"},
            headers=self.headers,
        )
        payload = response.get_json()
        self.assertEqual(response.status_code, 400)
        self.assertIn("unsupported platform", payload["error"].lower())

    def test_download_returns_file(self):
        with patch("vid.yt_dlp.YoutubeDL", FakeYoutubeDL):
            response = self.client.post(
                "/api/download",
                json={
                    "url": "https://www.youtube.com/watch?v=abc123",
                    "platform": "YouTube",
                    "download_type": "video",
                    "quality": "best",
                },
                headers=self.headers,
            )

        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"fake-video-content")
            self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
            self.assertIn("My Test Video.mp4", response.headers.get("Content-Disposition", ""))
            self.assertEqual(response.headers.get("Cache-Control"), "no-store")
        finally:
            response.close()

    def test_download_with_format_id_passes_to_yt_dlp(self):
        with patch("vid.yt_dlp.YoutubeDL", FakeYoutubeDL):
            response = self.client.post(
                "/api/download",
                json={
                    "url": "https://www.youtube.com/watch?v=abc123",
                    "platform": "YouTube",
                    "download_type": "video",
                    "quality": "best",
                    "format_id": "22",
                },
                headers=self.headers,
            )

        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(FakeYoutubeDL.last_options["format"], "22")
        finally:
            response.close()

    def test_ydl_options_force_ipv4_by_default(self):
        with patch.dict("os.environ", {}, clear=False):
            opts = vid.build_metadata_ydl_options(
                vid.MetadataRequest(url="https://www.facebook.com/watch/?v=12345", platform="facebook")
            )
        self.assertEqual(opts.get("source_address"), "0.0.0.0")

    def test_ydl_options_can_disable_ipv4_force(self):
        with patch.dict("os.environ", {"YTDLP_FORCE_IPV4": "0"}, clear=False):
            opts = vid.build_metadata_ydl_options(
                vid.MetadataRequest(url="https://www.facebook.com/watch/?v=12345", platform="facebook")
            )
        self.assertNotIn("source_address", opts)

    def test_map_download_error_for_socket_dns_failures(self):
        mapped = vid.map_download_error(Exception("WinError 10013 socket blocked"))
        self.assertEqual(mapped.status_code, 502)
        self.assertIn("dns/socket", mapped.message.lower())

    def test_download_returns_clear_message_for_socket_dns_errors(self):
        with patch("vid.yt_dlp.YoutubeDL", FakeNetworkErrorYoutubeDL):
            response = self.client.post(
                "/api/download",
                json={
                    "url": "https://www.facebook.com/watch/?v=12345",
                    "platform": "Facebook",
                    "download_type": "video",
                    "quality": "best",
                },
                headers=self.headers,
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 502)
        self.assertIn("dns/socket", payload["error"].lower())

    def test_map_download_error_for_facebook_auth_requirements(self):
        mapped = vid.map_download_error(Exception("facebook login checkpoint cookie required"), platform="facebook")
        self.assertEqual(mapped.status_code, 403)
        self.assertIn("only public videos are supported", mapped.message.lower())

    def test_map_download_error_for_tiktok_unavailable(self):
        mapped = vid.map_download_error(Exception("This video is not available"), platform="tiktok")
        self.assertEqual(mapped.status_code, 410)
        self.assertIn("unavailable", mapped.message.lower())

    def test_tiktok_ydl_options_match_required_configuration(self):
        opts = vid.get_platform_ydl_opts("tiktok")
        self.assertEqual(opts["quiet"], True)
        self.assertEqual(opts["no_warnings"], True)
        self.assertEqual(opts["socket_timeout"], 30)
        self.assertEqual(opts["retries"], 10)
        self.assertEqual(opts["fragment_retries"], 10)
        self.assertEqual(opts["skip_unavailable_fragments"], True)
        self.assertEqual(opts["format"], vid.TIKTOK_FORMAT_SELECTOR)
        self.assertEqual(opts["merge_output_format"], "mp4")
        self.assertEqual(opts["http_headers"]["Referer"], "https://www.tiktok.com/")
        self.assertEqual(opts["cookiefile"], None)
        self.assertNotIn("cookiesfrombrowser", opts)

    def test_map_download_error_for_instagram_login_required(self):
        mapped = vid.map_download_error(Exception("login_required"), platform="instagram")
        self.assertEqual(mapped.status_code, 403)
        self.assertIn("instagram content requires login", mapped.message.lower())

    def test_map_download_error_for_twitter_missing_tweet(self):
        mapped = vid.map_download_error(Exception("Could not find tweet"), platform="twitter")
        self.assertEqual(mapped.status_code, 404)
        self.assertIn("tweet/post could not be found", mapped.message.lower())

    def test_map_download_error_for_unsupported_url(self):
        mapped = vid.map_download_error(Exception("Unsupported URL"))
        self.assertEqual(mapped.status_code, 422)
        self.assertIn("not supported", mapped.message.lower())

    def test_map_download_error_for_platform_rate_limit(self):
        mapped = vid.map_download_error(Exception("HTTP Error 429: Too Many Requests"))
        self.assertEqual(mapped.status_code, 429)
        self.assertIn("rate limiting", mapped.message.lower())

    def test_map_download_error_for_ssl_failures(self):
        mapped = vid.map_download_error(Exception("SSL: CERTIFICATE_VERIFY_FAILED"))
        self.assertEqual(mapped.status_code, 502)
        self.assertIn("secure connection", mapped.message.lower())

    def test_facebook_download_options_ignore_format_id_and_use_app_id(self):
        job = vid.DownloadRequest(
            url="https://www.facebook.com/watch/?v=12345",
            platform="facebook",
            download_type="video",
            quality="best",
            format_id="22",
        )
        opts = vid.build_ydl_options(job, Path("/tmp/.download_tmp"))

        self.assertEqual(opts.get("format"), "best")
        self.assertEqual(opts.get("http_headers"), {"User-Agent": vid.DESKTOP_CHROME_120_UA})
        self.assertEqual(
            opts.get("extractor_args"),
            {"facebook": {"app_id": vid.FACEBOOK_EXTRACTOR_APP_ID}},
        )

    def test_facebook_download_retries_with_worst_format_on_format_error(self):
        FakeFacebookFormatRetryYoutubeDL.attempted_formats = []

        with patch("vid.yt_dlp.YoutubeDL", FakeFacebookFormatRetryYoutubeDL):
            response = self.client.post(
                "/api/download",
                json={
                    "url": "https://www.facebook.com/watch/?v=12345",
                    "platform": "Facebook",
                    "download_type": "video",
                    "quality": "best",
                    "format_id": "22",
                },
                headers=self.headers,
            )

        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"facebook-retry-content")
            self.assertEqual(FakeFacebookFormatRetryYoutubeDL.attempted_formats[:2], ["best", "worst"])
        finally:
            response.close()

    def test_facebook_metadata_retries_with_worst_format_on_format_error(self):
        FakeFacebookMetadataRetryYoutubeDL.attempted_formats = []

        with patch("vid.yt_dlp.YoutubeDL", FakeFacebookMetadataRetryYoutubeDL):
            response = self.client.post(
                "/api/metadata",
                json={"url": "https://www.facebook.com/reel/744372871141776/?mibextid=abcd1234"},
                headers=self.headers,
            )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["title"], "Facebook Meta Retry")
        self.assertEqual(FakeFacebookMetadataRetryYoutubeDL.attempted_formats[:2], ["best", "worst"])

    def test_rate_limit_blocks_sixth_successful_download(self):
        responses = []
        with patch("vid.yt_dlp.YoutubeDL", FakeYoutubeDL):
            for _ in range(6):
                response = self.client.post(
                    "/api/download",
                    json={
                        "url": "https://www.youtube.com/watch?v=abc123",
                        "platform": "YouTube",
                        "download_type": "video",
                        "quality": "best",
                    },
                    headers=self.headers,
                )
                responses.append(response)

        try:
            for response in responses[:5]:
                self.assertEqual(response.status_code, 200)
            self.assertEqual(responses[5].status_code, 429)
            payload = responses[5].get_json()
            self.assertEqual(payload["error"], "Rate limit exceeded")
            self.assertIn("retry_after", payload)
        finally:
            for response in responses:
                response.close()


if __name__ == "__main__":
    unittest.main()
