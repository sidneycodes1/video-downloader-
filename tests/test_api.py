import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import vid


class FakeYoutubeDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, _url, download=True):
        if not download:
            return {
                "id": "unit_test_id",
                "title": "My Test Video",
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
                        "acodec": "mp4a.40.2",
                        "filesize": 2_500_000,
                    },
                    {
                        "format_id": "137",
                        "ext": "mp4",
                        "height": 1080,
                        "vcodec": "avc1.640028",
                        "acodec": "none",
                        "filesize": 9_000_000,
                    },
                ],
            }

        output_template = self.options["outtmpl"]
        file_path = Path(output_template.replace("%(id)s", "unit_test_id").replace("%(ext)s", "mp4"))
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(b"fake-video-content")
        return {
            "id": "unit_test_id",
            "title": "My Test Video",
            "ext": "mp4",
        }


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
        self.assertIn("youtube", payload["supported_platforms"])

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

    def test_formats_probe_returns_quality_options(self):
        with patch("vid.yt_dlp.YoutubeDL", FakeYoutubeDL):
            response = self.client.post(
                "/api/download",
                json={
                    "mode": "formats",
                    "url": "https://www.youtube.com/watch?v=abc123",
                    "platform": "YouTube",
                    "download_type": "video",
                    "quality": "best",
                },
                headers=self.headers,
            )

        payload = response.get_json()

        self.assertEqual(response.status_code, 200)
        self.assertIn("formats", payload)
        self.assertGreaterEqual(len(payload["formats"]), 2)
        self.assertEqual(payload["formats"][0]["format_id"], "22")
        self.assertEqual(payload["formats"][0]["resolution"], "720p")

    def test_download_requires_format_id_in_download_mode(self):
        response = self.client.post(
            "/api/download",
            json={
                "mode": "download",
                "url": "https://www.youtube.com/watch?v=abc123",
                "platform": "YouTube",
                "download_type": "video",
            },
            headers=self.headers,
        )

        payload = response.get_json()
        self.assertEqual(response.status_code, 400)
        self.assertIn("format_id", payload["error"])

    def test_download_returns_file(self):
        with patch("vid.yt_dlp.YoutubeDL", FakeYoutubeDL):
            response = self.client.post(
                "/api/download",
                json={
                    "mode": "download",
                    "url": "https://www.youtube.com/watch?v=abc123",
                    "platform": "YouTube",
                    "download_type": "video",
                    "format_id": "22",
                    "job_id": "job_unittest",
                },
                headers=self.headers,
            )

        try:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.data, b"fake-video-content")
            self.assertIn("attachment", response.headers.get("Content-Disposition", ""))
            self.assertIn("My Test Video.mp4", response.headers.get("Content-Disposition", ""))
            self.assertEqual(response.headers.get("X-Download-Job-Id"), "job_unittest")
        finally:
            response.close()

    def test_rate_limit_blocks_sixth_successful_download(self):
        responses = []
        with patch("vid.yt_dlp.YoutubeDL", FakeYoutubeDL):
            for index in range(6):
                response = self.client.post(
                    "/api/download",
                    json={
                        "mode": "download",
                        "url": "https://www.youtube.com/watch?v=abc123",
                        "platform": "YouTube",
                        "download_type": "video",
                        "format_id": "22",
                        "job_id": f"job_rate_{index}",
                    },
                    headers=self.headers,
                )
                responses.append(response)

        try:
            for response in responses[:5]:
                self.assertEqual(response.status_code, 200)
            self.assertEqual(responses[5].status_code, 429)
            payload = responses[5].get_json()
            self.assertIn("too many successful downloads", payload["error"].lower())
        finally:
            for response in responses:
                response.close()


if __name__ == "__main__":
    unittest.main()
