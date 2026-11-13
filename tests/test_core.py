"""Focused unit tests for Drift's validation and helper functions."""

import importlib.util
import os
import socket
import subprocess
import json
import threading
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


if __name__ == "__main__":
    unittest.main()
