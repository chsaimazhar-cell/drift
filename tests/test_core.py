"""Focused unit tests for Drift's validation and helper functions."""

import importlib.util
import io
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

    def test_rejects_nonstandard_source_ports(self):
        self.assertFalse(drift.validate_public_url("https://youtube.com:8080/watch?v=x")[0])
        self.assertFalse(drift.validate_public_url("http://youtube.com:8443/watch?v=x")[0])
        self.assertFalse(drift.validate_public_url("https://youtube.com:invalid/watch?v=x")[0])

    def test_rejects_control_characters_in_source_urls(self):
        self.assertFalse(drift.validate_public_url("https://youtube.com/\nwatch?v=x")[0])

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

    def test_http_access_logs_redact_job_capabilities_and_query_strings(self):
        from contextlib import redirect_stdout
        output = io.StringIO()
        handler = object.__new__(drift.Handler)
        handler.client_address = ("127.0.0.1", 12345)
        with redirect_stdout(output):
            handler.log_message('"%s" %s %s', "GET /api/downloads/secret-job-token/file?sig=secret HTTP/1.1", "200", "-")
        logged = output.getvalue()
        self.assertNotIn("secret-job-token", logged)
        self.assertNotIn("sig=secret", logged)
        self.assertIn("[redacted-job-id]", logged)
        self.assertIn("[redacted]", logged)

    def test_http_handler_applies_idle_socket_timeout(self):
        class FakeConnection:
            timeout = None
            def settimeout(self, value):
                self.timeout = value

        handler = object.__new__(drift.Handler)
        handler.connection = FakeConnection()
        with patch.object(drift.BaseHTTPRequestHandler, "setup", lambda self: None), patch.object(
            drift, "HTTP_SOCKET_TIMEOUT_SECONDS", 17
        ):
            handler.setup()
        self.assertEqual(handler.connection.timeout, 17)

    def test_startup_marks_interrupted_jobs_failed_and_removes_partial_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "downloads"
            root.mkdir()
            database = Path(temp) / "jobs.sqlite3"
            partial = root / "drift-interrupted-job"
            partial.mkdir()
            (partial / "partial.mp4").write_bytes(b"partial")
            with patch.object(drift, "DB_PATH", database), patch.object(
                drift, "DOWNLOAD_ROOT", root
            ):
                drift.init_db()
                job = {
                    "id": "interrupted-job", "status": "downloading", "phase": "Downloading",
                    "url": "https://www.youtube.com/watch?v=test", "quality": "best",
                    "format": "MP4", "platform": "YouTube", "job_dir": str(partial),
                    "created_at": drift.now(), "updated_at": drift.now(),
                }
                self.assertTrue(drift.insert_job_if_capacity(job))
                recovered = drift.recover_interrupted_jobs()
                self.assertEqual(recovered, 1)
                self.assertFalse(partial.exists())
                stored = drift.get_job("interrupted-job")
                self.assertEqual(stored["status"], "failed")
                self.assertIn("restarted", stored["message"])

    def test_sqlite_repository_lifecycle_operations(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "jobs.sqlite3"
            with patch.object(drift, "DB_PATH", database):
                drift.init_db()
                base = {
                    "status": "queued", "phase": "Queued", "progress": 0,
                    "downloaded_bytes": 0, "total_bytes": 0, "url": "https://youtube.com/watch?v=x",
                    "quality": "best", "format": "MP4", "platform": "YouTube",
                    "created_at": drift.now(), "updated_at": drift.now(),
                }
                drift.JOB_REPOSITORY.insert(dict(base, id="repo-active"))
                self.assertEqual(drift.JOB_REPOSITORY.active_count(), 1)
                drift.JOB_REPOSITORY.cancel_active("repo-active")
                self.assertEqual(drift.get_job("repo-active")["status"], "cancelled")
                drift.update_job("repo-active", status="failed", updated_at=1)
                expired = drift.JOB_REPOSITORY.expired_jobs(drift.now() + 10)
                self.assertEqual([job["id"] for job in expired["expired"]], ["repo-active"])
                self.assertIsNone(drift.get_job("repo-active"))
                self.assertEqual(expired["tracked"], [])

    def test_media_probe_has_a_hard_timeout(self):
        timeout = subprocess.TimeoutExpired(cmd="ffmpeg", timeout=30)
        with patch.object(drift, "find_ffmpeg", return_value="ffmpeg"), patch.object(
            drift.subprocess, "run", side_effect=timeout
        ):
            with self.assertRaisesRegex(RuntimeError, "inspection took too long"):
                drift.probe_media(Path("slow-media.mp4"))

    def test_media_conversion_has_a_hard_timeout(self):
        class SlowProcess:
            returncode = None

            def __init__(self):
                self.terminated = False
                self.killed = False

            def communicate(self, timeout=None):
                if self.terminated or self.killed:
                    return ("", "")
                raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout)

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        process = SlowProcess()
        info = {
            "video": [{"codec": "mpeg4", "pix_fmt": "yuv420p", "profile": "Simple",
                       "w": 64, "h": 64}],
            "audio": [],
            "container": "avi",
        }
        with tempfile.TemporaryDirectory() as temp:
            media = Path(temp) / "source.avi"
            media.write_bytes(b"source")
            with patch.object(drift, "find_ffmpeg", return_value="ffmpeg"), patch.object(
                drift, "probe_media", return_value=info
            ), patch.object(drift.subprocess, "Popen", return_value=process), patch.object(
                drift, "FFMPEG_CONVERSION_TIMEOUT_SECONDS", 30
            ), patch.object(drift.time, "monotonic", side_effect=[0, 31, 31]):
                with self.assertRaisesRegex(RuntimeError, "conversion exceeded"):
                    drift.ensure_compatible_mp4(media, "slow-conversion")
            self.assertTrue(process.terminated)

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

    def test_storage_budget_reserves_active_jobs_and_counts_ready_files(self):
        with tempfile.TemporaryDirectory() as temp:
            test_db = Path(temp) / "storage.sqlite3"
            with patch.object(drift, "DB_PATH", test_db), patch.object(drift, "DOWNLOAD_ROOT", Path(temp)), patch.object(drift, "MAX_TOTAL_JOBS", 5), patch.object(drift, "MAX_DOWNLOAD_BYTES", 100), patch.object(drift, "MAX_STORAGE_BYTES", 500), patch.object(drift.shutil, "disk_usage", return_value=type("Usage", (), {"free": 10000})()):
                drift.init_db()
                base = {
                    "status": "queued", "phase": "Queued",
                    "url": "https://www.youtube.com/watch?v=one", "quality": "best",
                    "format": "MP4", "platform": "YouTube",
                    "created_at": drift.now(), "updated_at": drift.now(),
                }
                self.assertTrue(drift.insert_job_if_capacity(dict(base, id="active-1")))
                self.assertTrue(drift.insert_job_if_capacity(dict(base, id="active-2")))
                self.assertFalse(drift.insert_job_if_capacity(dict(base, id="active-3")))
                with drift.db() as conn:
                    conn.execute("UPDATE jobs SET status='ready', file_size=80 WHERE id='active-1'")
                self.assertTrue(drift.insert_job_if_capacity(dict(base, id="active-3")))
                with drift.db() as conn:
                    conn.execute("UPDATE jobs SET status='ready', file_size=80 WHERE id='active-2'")
                self.assertFalse(drift.insert_job_if_capacity(dict(base, id="active-4")))

    def test_storage_budget_rejects_when_disk_free_space_is_low(self):
        with tempfile.TemporaryDirectory() as temp:
            test_db = Path(temp) / "storage.sqlite3"
            with patch.object(drift, "DB_PATH", test_db), patch.object(drift, "DOWNLOAD_ROOT", Path(temp)), patch.object(drift, "MAX_TOTAL_JOBS", 5), patch.object(drift, "MAX_DOWNLOAD_BYTES", 100), patch.object(drift, "MAX_STORAGE_BYTES", 1000), patch.object(drift.shutil, "disk_usage", return_value=type("Usage", (), {"free": 99})()):
                drift.init_db()
                job = {
                    "id": "low-disk", "status": "queued", "phase": "Queued",
                    "url": "https://www.youtube.com/watch?v=one", "quality": "best",
                    "format": "MP4", "platform": "YouTube",
                    "created_at": drift.now(), "updated_at": drift.now(),
                }
                self.assertFalse(drift.insert_job_if_capacity(job))

    def test_json_body_requires_content_type_and_valid_length(self):
        handler = object.__new__(drift.Handler)
        handler.headers = {"Content-Type": "text/plain", "Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        with self.assertRaisesRegex(ValueError, "application/json"):
            handler.read_json()

        handler.headers = {"Content-Type": "application/json", "Content-Length": "-1"}
        with self.assertRaisesRegex(ValueError, "empty"):
            handler.read_json()

        handler.headers = {"Content-Type": "application/json", "Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            handler.read_json()

        handler.headers = {"Content-Type": "application/json", "Content-Length": "2"}
        handler.rfile = io.BytesIO(b"{}")
        self.assertEqual(handler.read_json(), {})

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

    def test_provider_error_urls_do_not_leak_query_tokens_or_credentials(self):
        message = "Failed https://user:pass@cdn.example/video?token=secret&sig=private#fragment"
        cleaned = drift.clean_error(RuntimeError(message))
        self.assertNotIn("secret", cleaned)
        self.assertNotIn("private", cleaned)
        self.assertNotIn("user", cleaned)
        self.assertNotIn("pass", cleaned)
        self.assertIn("[redacted URL]", cleaned)

    def test_cors_is_closed_by_default(self):
        class FakeHandler:
            headers = {"Origin": "https://attacker.example"}
            sent = []
            def send_header(self, key, value):
                self.sent.append((key, value))

        handler = FakeHandler()
        with patch.dict(os.environ, {}, clear=True):
            drift.Handler.send_cors(handler)
        self.assertFalse(any(key == "Access-Control-Allow-Origin" for key, _ in handler.sent))

    def test_cors_only_allows_configured_origin(self):
        class FakeHandler:
            headers = {"Origin": "https://attacker.example"}
            sent = []
            def send_header(self, key, value):
                self.sent.append((key, value))

        handler = FakeHandler()
        with patch.dict(os.environ, {"DRIFT_ALLOWED_ORIGIN": "https://drift.example"}):
            drift.Handler.send_cors(handler)
        self.assertNotIn(("Access-Control-Allow-Origin", "https://drift.example"), handler.sent)
        self.assertIn(("Vary", "Origin"), handler.sent)

        handler.sent = []
        handler.headers = {"Origin": "https://drift.example"}
        with patch.dict(os.environ, {"DRIFT_ALLOWED_ORIGIN": "https://drift.example"}):
            drift.Handler.send_cors(handler)
        self.assertIn(("Access-Control-Allow-Origin", "https://drift.example"), handler.sent)

    def test_cors_wildcard_is_explicit_and_not_reflected(self):
        class FakeHandler:
            headers = {"Origin": "https://any.example"}
            sent = []
            def send_header(self, key, value):
                self.sent.append((key, value))

        handler = FakeHandler()
        with patch.dict(os.environ, {"DRIFT_ALLOWED_ORIGIN": "*"}):
            drift.Handler.send_cors(handler)
        self.assertIn(("Access-Control-Allow-Origin", "*"), handler.sent)
        self.assertNotIn(("Access-Control-Allow-Origin", "https://any.example"), handler.sent)

    def test_client_ip_ignores_forwarded_header_by_default(self):
        class FakeHandler:
            client_address = ("203.0.113.10", 12345)
            headers = {"X-Forwarded-For": "198.51.100.25"}

        with patch.dict(os.environ, {"DRIFT_TRUST_PROXY": ""}):
            self.assertEqual(drift.client_key(FakeHandler()), "203.0.113.10")

    def test_client_ip_uses_forwarded_header_only_when_trusted(self):
        class FakeHandler:
            client_address = ("203.0.113.10", 12345)
            headers = {"X-Forwarded-For": "198.51.100.25, 203.0.113.20"}

        with patch.dict(os.environ, {"DRIFT_TRUST_PROXY": "true"}):
            self.assertEqual(drift.client_key(FakeHandler()), "198.51.100.25")

    def test_client_ip_falls_back_when_trusted_header_is_invalid(self):
        class FakeHandler:
            client_address = ("203.0.113.10", 12345)
            headers = {"X-Forwarded-For": "not-an-ip"}

        with patch.dict(os.environ, {"DRIFT_TRUST_PROXY": "1"}):
            self.assertEqual(drift.client_key(FakeHandler()), "203.0.113.10")

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
