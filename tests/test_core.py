"""Focused unit tests for Drift's validation and helper functions."""

import importlib.util
import os
import socket
import subprocess
import json
import threading
import uuid
from urllib.request import urlopen
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_ROOT = Path(tempfile.mkdtemp(prefix="drift-unit-tests-"))
os.environ["DRIFT_DOWNLOAD_ROOT"] = str(TEST_ROOT)
os.environ["DRIFT_DB_PATH"] = str(TEST_ROOT / "test.sqlite3")

SOURCE = Path(__file__).resolve().parents[1] / "drift.py"
SPEC = importlib.util.spec_from_file_location("drift_app_under_test", SOURCE)
drift = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(drift)


def public_dns_result():
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


class DriftHelperTests(unittest.TestCase):
    def setUp(self):
        drift.RATE_WINDOWS.clear()

    def test_rejects_empty_and_non_http_urls(self):
        self.assertFalse(drift.validate_public_url("")[0])
        self.assertFalse(drift.validate_public_url("ftp://youtube.com/video")[0])
        self.assertFalse(drift.validate_public_url(None)[0])

    def test_rejects_unsupported_hosts(self):
        self.assertFalse(drift.validate_public_url("https://example.org/video")[0])

    def test_rejects_local_ip_addresses(self):
        self.assertFalse(drift.validate_public_url("http://127.0.0.1/admin")[0])
        self.assertFalse(drift.validate_public_url("http://192.168.1.10/")[0])

    def test_rejects_url_credentials(self):
        self.assertFalse(drift.validate_public_url("https://user:pass@youtube.com/watch?v=x")[0])

    def test_rejects_supported_host_resolving_to_private_ip(self):
        private_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]
        with patch.object(drift.socket, "getaddrinfo", return_value=private_dns):
            valid, message = drift.validate_public_url("https://www.youtube.com/watch?v=x")
        self.assertFalse(valid)
        self.assertIn("Private or internal", message)

    def test_accepts_supported_host_with_public_dns(self):
        with patch.object(drift.socket, "getaddrinfo", return_value=public_dns_result()):
            valid, parsed = drift.validate_public_url("https://www.youtube.com/watch?v=x")
        self.assertTrue(valid)
        self.assertEqual(parsed.hostname, "www.youtube.com")

    def test_quality_normalization_and_selector(self):
        self.assertEqual(drift.normalize_quality("720P"), "720p")
        self.assertEqual(drift.normalize_quality("9999p"), "best")
        self.assertIn("height<=720", drift.format_selector("720p"))
        self.assertIn("bestvideo", drift.format_selector("best"))

    def test_safe_filename_removes_path_and_control_characters(self):
        result = drift.safe_filename("../unsafe:name?.mp4")
        self.assertNotIn("/", result)
        self.assertNotIn(":", result)
        self.assertTrue(result.endswith(".mp4"))

    def test_job_queue_capacity_is_enforced_atomically(self):
        with tempfile.TemporaryDirectory() as temp:
            test_db = Path(temp) / "queue.sqlite3"
            with patch.object(drift, "DB_PATH", test_db), patch.object(drift, "MAX_TOTAL_JOBS", 1):
                drift.init_db()
                first = {
                    "id": "queue-test-one", "status": "queued", "phase": "Queued",
                    "url": "https://www.youtube.com/watch?v=one", "quality": "best",
                    "format": "MP4", "platform": "YouTube",
                    "created_at": drift.now(), "updated_at": drift.now(),
                }
                second = dict(first, id="queue-test-two")
                self.assertTrue(drift.insert_job_if_capacity(first))
                self.assertFalse(drift.insert_job_if_capacity(second))
                with drift.db() as conn:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM jobs WHERE status IN ('queued','downloading','processing')"
                    ).fetchone()[0]
                self.assertEqual(count, 1)

    def test_public_job_hides_internal_storage_paths(self):
        job = {
            "id": "job123456",
            "status": "ready",
            "url": "https://www.youtube.com/watch?v=test",
            "file_path": "/srv/drift/private/job.mp4",
            "job_dir": "/srv/drift/private",
            "file_size": 123,
        }
        public = drift.public_job(job)
        self.assertEqual(public["id"], "job123456")
        self.assertEqual(public["file_size"], 123)
        self.assertNotIn("file_path", public)
        self.assertNotIn("job_dir", public)

    def test_error_messages_are_sanitized(self):
        self.assertIn("403/429", drift.clean_error(RuntimeError("HTTP Error 429")))

    def test_rate_limit_blocks_requests_over_the_limit(self):
        class FakeHandler:
            client_address = ("203.0.113.10", 12345)

        handler = FakeHandler()
        with patch.object(drift, "now", return_value=1000.0):
            self.assertTrue(drift.rate_limit(handler, "unit-test", 2, 60))
            self.assertTrue(drift.rate_limit(handler, "unit-test", 2, 60))
            self.assertFalse(drift.rate_limit(handler, "unit-test", 2, 60))


    def test_http_server_health_and_frontend_smoke(self):
        drift.init_db()
        server = drift.ThreadingHTTPServer(("127.0.0.1", 0), drift.Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base_url + "/api/health", timeout=5) as response:
                self.assertEqual(response.status, 200)
                health = json.loads(response.read().decode("utf-8"))
            self.assertEqual(health["status"], "ok")
            with urlopen(base_url + "/", timeout=5) as response:
                self.assertEqual(response.status, 200)
                page = response.read().decode("utf-8")
            self.assertIn("<title>", page)
            with urlopen(base_url + "/css/style.css", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("--accent", response.read().decode("utf-8"))
            with urlopen(base_url + "/js/app.js", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("API_BASE", response.read().decode("utf-8"))
            head_request = __import__("urllib.request", fromlist=["Request"]).Request(
                base_url + "/css/style.css", method="HEAD"
            )
            with urlopen(head_request, timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertGreater(int(response.headers["Content-Length"]), 0)
            handler = object.__new__(drift.Handler)
            self.assertIsNone(handler.static_file_path("/%2e%2e/drift.py"))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)

    def test_non_mp4_media_is_converted_to_compatible_mp4(self):
        ffmpeg = drift.find_ffmpeg()
        if not ffmpeg:
            self.skipTest("FFmpeg is not available in this test environment.")
        encoders = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        if "libx264" not in encoders:
            self.skipTest("This FFmpeg build does not include the libx264 encoder.")

        drift.init_db()
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "source.avi"
            subprocess.run(
                [
                    ffmpeg, "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=64x64:d=1", "-c:v", "mpeg4",
                    "-an", str(source),
                ],
                capture_output=True,
                check=True,
            )
            converted = drift.ensure_compatible_mp4(source, "unit-conversion")
            self.assertTrue(converted.is_file())
            self.assertEqual(converted.suffix.lower(), ".mp4")
            media_info = drift.probe_media(converted)
            self.assertEqual(media_info["video"][0]["codec"], "h264")
            self.assertEqual(media_info["video"][0]["pix_fmt"], "yuv420p")
            self.assertIn("mp4", media_info["container"].split(","))

    def test_download_file_streaming_and_http_range(self):
        drift.init_db()
        with tempfile.TemporaryDirectory() as temp:
            media_path = Path(temp) / "sample.mp4"
            media_bytes = b"0123456789abcdef"
            media_path.write_bytes(media_bytes)
            job_id = uuid.uuid4().hex
            timestamp = drift.now()
            drift.insert_job({
                "id": job_id,
                "status": "ready",
                "phase": "Ready",
                "progress": 100,
                "downloaded_bytes": len(media_bytes),
                "total_bytes": len(media_bytes),
                "speed": None,
                "eta": None,
                "url": "https://www.youtube.com/watch?v=test",
                "quality": "best",
                "format": "MP4",
                "platform": "YouTube",
                "title": "Sample file",
                "thumbnail": None,
                "filename": "Sample_file.mp4",
                "file_size": len(media_bytes),
                "message": "Ready",
                "error": None,
                "created_at": timestamp,
                "updated_at": timestamp,
                "completed_at": timestamp,
                "job_dir": temp,
                "file_path": str(media_path),
                "duration": "0:01",
            })
            server = drift.ThreadingHTTPServer(("127.0.0.1", 0), drift.Handler)
            worker = threading.Thread(target=server.serve_forever, daemon=True)
            worker.start()
            base_url = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(base_url + f"/api/downloads/{job_id}/file", timeout=5) as response:
                    self.assertEqual(response.status, 200)
                    self.assertEqual(response.read(), media_bytes)
                    self.assertEqual(response.headers.get("Accept-Ranges"), "bytes")

                request = __import__("urllib.request", fromlist=["Request"]).Request(
                    base_url + f"/api/downloads/{job_id}/file",
                    headers={"Range": "bytes=2-5"},
                )
                with urlopen(request, timeout=5) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.read(), b"2345")
                    self.assertEqual(response.headers.get("Content-Range"), "bytes 2-5/16")
            finally:
                server.shutdown()
                server.server_close()
                worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
