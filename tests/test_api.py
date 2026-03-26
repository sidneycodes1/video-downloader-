import unittest
import uuid
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


class VideoDownloaderApiTests(unittest.TestCase):
    def setUp(self):
        vid.app.config["TESTING"] = True
        self.client = vid.app.test_client()
        self.client_ip = f"test-client-{uuid.uuid4().hex}"
        self.headers = {"X-Forwarded-For": self.client_ip}

    def tearDown(self):
        if hasattr(vid.limiter, "reset"):
            vid.limiter.reset()

    def test_health_endpoint(self):
        response = self.client.get("/api/health")
        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(payload["status"], "ok")
        self.assertIn("/api/download", payload["endpoints"])
        self.assertIn("/api/metadata", payload["endpoints"])
        self.assertIn("/history", payload["endpoints"])

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

    def test_validate_supported_url_normalizes_facebook_host_and_tracking_params(self):
        url, platform = vid.validate_supported_url(
            "https://m.facebook.com/reel/744372871141776/?mibextid=abcd1234&ref=share"
        )

        self.assertEqual(platform, "facebook")
        self.assertEqual(url, "https://www.facebook.com/watch/?v=744372871141776")

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
