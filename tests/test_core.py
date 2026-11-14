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
            ), patch.object(drift, "MEDIA_STORAGE", drift.LocalMediaStorage(root)):
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

    def test_sqlite_migrations_create_fresh_schema_and_record_version(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "fresh.sqlite3"
            with patch.object(drift, "DB_PATH", database):
                drift.JOB_REPOSITORY.initialize()
                with drift.db() as conn:
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
                    indexes = {row["name"] for row in conn.execute("PRAGMA index_list(jobs)")}
                self.assertEqual(version, drift.JOB_REPOSITORY.SCHEMA_VERSION)
                self.assertIn("id", columns)
                self.assertIn("file_path", columns)
                self.assertIn("idx_jobs_updated", indexes)
                self.assertIn("idx_jobs_status", indexes)

    def test_sqlite_migrations_adopt_existing_schema_without_losing_jobs(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "legacy.sqlite3"
            with patch.object(drift, "DB_PATH", database):
                drift.JOB_REPOSITORY.initialize()
                legacy_job = {
                    "id": "legacy-preserved", "status": "ready", "phase": "Ready",
                    "url": "https://youtube.com/watch?v=legacy", "quality": "best",
                    "format": "MP4", "platform": "YouTube",
                    "created_at": drift.now(), "updated_at": drift.now(),
                }
                drift.JOB_REPOSITORY.insert(legacy_job)
                with drift.db() as conn:
                    conn.execute("PRAGMA user_version = 0")
                drift.JOB_REPOSITORY.initialize()
                self.assertEqual(drift.get_job("legacy-preserved")["status"], "ready")
                with drift.db() as conn:
                    self.assertEqual(
                        conn.execute("PRAGMA user_version").fetchone()[0],
                        drift.JOB_REPOSITORY.SCHEMA_VERSION,
                    )

    def test_sqlite_migrations_are_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "idempotent.sqlite3"
            with patch.object(drift, "DB_PATH", database):
                drift.JOB_REPOSITORY.initialize()
                drift.JOB_REPOSITORY.initialize()
                with drift.db() as conn:
                    self.assertEqual(
                        conn.execute("PRAGMA user_version").fetchone()[0],
                        drift.JOB_REPOSITORY.SCHEMA_VERSION,
                    )
                    self.assertEqual(
                        conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='jobs'").fetchone()[0],
                        1,
                    )

    def test_sqlite_migration_failure_rolls_back_schema_and_version(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "rollback.sqlite3"

            def failing_migration(conn):
                conn.execute("CREATE TABLE migration_probe (id INTEGER PRIMARY KEY)")
                raise RuntimeError("simulated migration failure")

            with patch.object(drift, "DB_PATH", database), patch.object(
                drift.SQLiteJobRepository, "MIGRATIONS", ((1, failing_migration),)
            ):
                with self.assertRaisesRegex(RuntimeError, "simulated migration failure"):
                    drift.JOB_REPOSITORY.initialize()
                with drift.db() as conn:
                    version = conn.execute("PRAGMA user_version").fetchone()[0]
                    probe = conn.execute(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='migration_probe'"
                    ).fetchone()[0]
                self.assertEqual(version, 0)
                self.assertEqual(probe, 0)

    def test_sqlite_repository_rejects_newer_schema_versions(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "future.sqlite3"
            with patch.object(drift, "DB_PATH", database):
                with drift.db() as conn:
                    conn.execute(f"PRAGMA user_version = {drift.JOB_REPOSITORY.SCHEMA_VERSION + 1}")
                with self.assertRaisesRegex(RuntimeError, "newer than this Drift backend supports"):
                    drift.JOB_REPOSITORY.initialize()

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
                expired = drift.JOB_REPOSITORY.expired_jobs(2_000_000_000)
                self.assertEqual([job["id"] for job in expired["expired"]], ["repo-active"])
                self.assertIsNone(drift.get_job("repo-active"))
                self.assertEqual(expired["tracked"], [])

    def test_sqlite_repository_read_update_ready_and_recovery_contract(self):
        with tempfile.TemporaryDirectory() as temp:
            database = Path(temp) / "repository-contract.sqlite3"
            with patch.object(drift, "DB_PATH", database):
                drift.init_db()
                repository = drift.JOB_REPOSITORY
                base = {
                    "status": "queued", "phase": "Queued", "progress": 0,
                    "downloaded_bytes": 0, "total_bytes": 0,
                    "url": "https://youtube.com/watch?v=contract", "quality": "best",
                    "format": "MP4", "platform": "YouTube",
                    "created_at": drift.now(), "updated_at": drift.now(),
                }
                repository.insert(dict(base, id="repo-contract-ready"))
                self.assertEqual(repository.get("repo-contract-ready")["status"], "queued")
                repository.update("repo-contract-ready", {"phase": "Preparing", "progress": 12})
                updated = repository.get("repo-contract-ready")
                self.assertEqual(updated["phase"], "Preparing")
                self.assertEqual(updated["progress"], 12)
                self.assertTrue(repository.mark_ready(
                    "repo-contract-ready",
                    {"phase": "Ready", "progress": 100, "filename": "ready.mp4"},
                ))
                self.assertEqual(repository.get("repo-contract-ready")["status"], "ready")
                self.assertFalse(repository.mark_ready(
                    "repo-contract-ready", {"phase": "Ready again"}
                ))
                self.assertIsNone(repository.get("missing-repository-job"))

                repository.insert(dict(base, id="repo-contract-interrupted"))
                interrupted = repository.recover_interrupted()
                self.assertEqual([job["id"] for job in interrupted], ["repo-contract-interrupted"])
                recovered = repository.get("repo-contract-interrupted")
                self.assertEqual(recovered["status"], "failed")
                self.assertIn("restarted", recovered["message"].lower())

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


    def test_private_media_object_store_contract_with_fake_adapter(self):
        class FakePrivateObjectStore:
            def __init__(self):
                self.objects = {}

            def put_file(self, source, object_key, content_type, filename):
                payload = source.read_bytes()
                self.objects[object_key] = {
                    "payload": payload,
                    "content_type": content_type,
                    "filename": filename,
                }
                return drift.StoredMediaObject(
                    object_key=object_key,
                    size_bytes=len(payload),
                    content_type=content_type,
                    filename=filename,
                )

            def create_download_url(self, object_key, expires_in_seconds):
                if object_key not in self.objects or not 1 <= expires_in_seconds <= 900:
                    raise ValueError("A stored object and short expiry are required.")
                return f"https://private-storage.invalid/signed/{object_key}?ttl={expires_in_seconds}"

            def delete_object(self, object_key):
                self.objects.pop(object_key, None)

            def delete_expired_objects(self, cutoff_timestamp):
                expired = [
                    key for key, value in self.objects.items()
                    if value.get("expires_at", float("inf")) <= cutoff_timestamp
                ]
                for key in expired:
                    del self.objects[key]
                return len(expired)

        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp) / "finished.mp4"
            source.write_bytes(b"completed-media")
            store = FakePrivateObjectStore()
            self.assertIsInstance(store, drift.PrivateMediaObjectStore)
            stored = store.put_file(
                source, "jobs/job-capability/media.mp4", "video/mp4", "finished.mp4"
            )
            self.assertEqual(stored.size_bytes, len(b"completed-media"))
            self.assertEqual(stored.content_type, "video/mp4")
            signed_url = store.create_download_url(stored.object_key, 300)
            self.assertIn("ttl=300", signed_url)
            self.assertEqual(store.objects[stored.object_key]["payload"], b"completed-media")
            store.delete_object(stored.object_key)
            self.assertNotIn(stored.object_key, store.objects)

    def test_local_media_storage_workspace_lifecycle(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = drift.LocalMediaStorage(Path(temp))
            workspace = storage.create_workspace("storage-test")
            source = workspace / "source.webm"
            source.write_bytes(b"media-data")
            self.assertEqual(storage.find_media(workspace), source)
            self.assertEqual(storage.size(source), 10)
            final_path = workspace / "final.mp4"
            self.assertEqual(storage.finalize(source, final_path), final_path)
            self.assertTrue(storage.is_file(final_path))
            with storage.open_file(final_path) as stream:
                self.assertEqual(stream.read(), b"media-data")
            self.assertEqual(storage.size(final_path), 10)
            storage.delete_workspace(workspace)
            self.assertFalse(workspace.exists())

    def test_local_media_storage_prefers_complete_media_over_partial_fragments(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            storage = drift.LocalMediaStorage(root)
            workspace = storage.create_workspace("find-media")
            partial = workspace / "video.f137.mp4"
            partial.write_bytes(b"x" * 100)
            interrupted = workspace / "video.mp4.part"
            interrupted.write_bytes(b"x" * 200)
            complete = workspace / "video.mp4"
            complete.write_bytes(b"x" * 10)
            self.assertEqual(storage.find_media(workspace), complete)

    def test_local_media_storage_falls_back_to_partial_media_when_no_complete_file_exists(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            storage = drift.LocalMediaStorage(root)
            workspace = storage.create_workspace("find-media-fallback")
            partial = workspace / "video.f137.mp4"
            partial.write_bytes(b"partial")
            self.assertEqual(storage.find_media(workspace), partial)

    def test_local_media_storage_cleanup_stale_removes_only_untracked_owned_directories(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            storage = drift.LocalMediaStorage(root)
            stale = root / "drift-stale-job"
            tracked = root / "drift-tracked-job"
            unrelated = root / "other-stale-directory"
            for directory in (stale, tracked, unrelated):
                directory.mkdir()
                (directory / "file.bin").write_bytes(b"data")
            old_time = drift.now() - 3600
            for directory in (stale, tracked, unrelated):
                os.utime(directory, (old_time, old_time))
            storage.cleanup_stale({tracked.name}, drift.now() - 60)
            self.assertFalse(stale.exists())
            self.assertTrue(tracked.exists())
            self.assertTrue(unrelated.exists())

    def test_local_media_storage_reports_available_disk_space(self):
        with tempfile.TemporaryDirectory() as temp:
            storage = drift.LocalMediaStorage(Path(temp))
            self.assertGreaterEqual(storage.free_bytes(), 0)

    def test_local_media_storage_removes_only_owned_interrupted_workspace(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            storage = drift.LocalMediaStorage(root)
            owned = root / "drift-interrupted"
            owned.mkdir()
            external = root / "other-directory"
            external.mkdir()
            storage.remove_interrupted(str(owned))
            storage.remove_interrupted(str(external))
            self.assertFalse(owned.exists())
            self.assertTrue(external.exists())


    def test_repository_queue_claim_is_atomic_and_enforces_availability(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(drift, "DB_PATH", Path(temp) / "queue.sqlite3"):
                drift.init_db()
                job = {
                    "id": "claim-once", "status": "queued", "phase": "Queued",
                    "url": "https://youtube.com/watch?v=claim", "quality": "best",
                    "format": "MP4", "platform": "YouTube",
                    "created_at": 10, "updated_at": 10, "available_at": 50,
                }
                drift.JOB_REPOSITORY.insert(job)
                self.assertIsNone(drift.JOB_REPOSITORY.claim_job("claim-once", "worker-a", timestamp=49))
                claimed = drift.JOB_REPOSITORY.claim_job("claim-once", "worker-a", timestamp=50)
                self.assertEqual(claimed["lease_owner"], "worker-a")
                self.assertEqual(claimed["attempts"], 1)
                self.assertIsNone(drift.JOB_REPOSITORY.claim_job("claim-once", "worker-b", timestamp=51))

    def test_repository_queue_lease_renewal_and_acknowledgement(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(drift, "DB_PATH", Path(temp) / "lease.sqlite3"):
                drift.init_db()
                job = {
                    "id": "lease-job", "status": "queued", "phase": "Queued",
                    "url": "https://youtube.com/watch?v=lease", "quality": "best",
                    "format": "MP4", "platform": "YouTube",
                    "created_at": 10, "updated_at": 10,
                }
                drift.JOB_REPOSITORY.insert(job)
                drift.JOB_REPOSITORY.claim_job("lease-job", "worker-a", lease_seconds=10, timestamp=20)
                self.assertTrue(drift.JOB_REPOSITORY.renew_lease(
                    "lease-job", "worker-a", lease_seconds=20, timestamp=25
                ))
                self.assertFalse(drift.JOB_REPOSITORY.renew_lease(
                    "lease-job", "worker-b", timestamp=26
                ))
                self.assertFalse(drift.JOB_REPOSITORY.acknowledge_job("lease-job", "worker-a", timestamp=27))
                drift.update_job("lease-job", status="ready")
                self.assertTrue(drift.JOB_REPOSITORY.acknowledge_job("lease-job", "worker-a", timestamp=28))
                stored = drift.get_job("lease-job")
                self.assertIsNone(stored["lease_owner"])
                self.assertIsNone(stored["lease_expires_at"])

    def test_repository_queue_retry_and_expired_lease_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(drift, "DB_PATH", Path(temp) / "retry.sqlite3"):
                drift.init_db()
                base = {
                    "status": "queued", "phase": "Queued",
                    "url": "https://youtube.com/watch?v=retry", "quality": "best",
                    "format": "MP4", "platform": "YouTube",
                    "created_at": 1, "updated_at": 1,
                }
                drift.JOB_REPOSITORY.insert(dict(base, id="retry-manual"))
                drift.JOB_REPOSITORY.claim_job("retry-manual", "worker-a", timestamp=10)
                self.assertTrue(drift.JOB_REPOSITORY.retry_job(
                    "retry-manual", "worker-a", delay_seconds=15, timestamp=11
                ))
                self.assertIsNone(drift.JOB_REPOSITORY.claim_job(
                    "retry-manual", "worker-b", timestamp=25
                ))
                self.assertIsNotNone(drift.JOB_REPOSITORY.claim_job(
                    "retry-manual", "worker-b", timestamp=26
                ))

                drift.JOB_REPOSITORY.insert(dict(base, id="lease-recover"))
                drift.JOB_REPOSITORY.claim_job(
                    "lease-recover", "worker-old", lease_seconds=5, timestamp=30
                )
                recovered = drift.JOB_REPOSITORY.recover_expired_leases(
                    max_attempts=3, timestamp=36
                )
                self.assertEqual([row["id"] for row in recovered], ["lease-recover"])
                recovered_job = drift.get_job("lease-recover")
                self.assertEqual(recovered_job["status"], "queued")
                self.assertIsNone(recovered_job["lease_owner"])

    def test_repository_queue_marks_exhausted_expired_lease_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(drift, "DB_PATH", Path(temp) / "exhausted.sqlite3"):
                drift.init_db()
                job = {
                    "id": "exhausted-job", "status": "queued", "phase": "Queued",
                    "url": "https://youtube.com/watch?v=exhausted", "quality": "best",
                    "format": "MP4", "platform": "YouTube",
                    "created_at": 1, "updated_at": 1,
                }
                drift.JOB_REPOSITORY.insert(job)
                drift.JOB_REPOSITORY.claim_job("exhausted-job", "worker-a", lease_seconds=5, timestamp=10)
                self.assertEqual(
                    len(drift.JOB_REPOSITORY.recover_expired_leases(max_attempts=1, timestamp=16)), 1
                )
                self.assertEqual(drift.get_job("exhausted-job")["status"], "failed")

    def test_local_job_queue_claims_runs_and_acknowledges_terminal_job(self):
        queue = drift.LocalThreadPoolJobQueue(max_workers=1)
        job = {"id": "worker-lifecycle", "url": "https://youtube.com/watch?v=worker", "quality": "best"}
        claimed = dict(job, status="downloading")
        terminal = dict(job, status="ready")
        with patch.object(drift, "JOB_REPOSITORY") as repository, patch.object(
            drift, "run_download"
        ) as worker:
            repository.get.return_value = job
            repository.claim_job.return_value = claimed
            repository.get.side_effect = [job, terminal]
            try:
                future = queue.enqueue(job["id"])
                future.result(timeout=3)
                repository.claim_job.assert_called_once()
                worker.assert_called_once_with(job["id"], job["url"], job["quality"])
                repository.acknowledge_job.assert_called_once()
                repository.retry_job.assert_not_called()
            finally:
                queue.shutdown(wait=True, cancel_pending=True)

    def test_local_job_queue_does_not_run_job_when_claim_is_lost(self):
        queue = drift.LocalThreadPoolJobQueue(max_workers=1)
        job = {"id": "worker-claim-lost", "url": "https://youtube.com/watch?v=lost", "quality": "best"}
        with patch.object(drift, "JOB_REPOSITORY") as repository, patch.object(
            drift, "run_download"
        ) as worker:
            repository.get.return_value = job
            repository.claim_job.return_value = None
            try:
                queue.enqueue(job["id"]).result(timeout=3)
                worker.assert_not_called()
                repository.acknowledge_job.assert_not_called()
            finally:
                queue.shutdown(wait=True, cancel_pending=True)

    def test_local_job_queue_dispatches_job_id_and_shuts_down(self):
        queue = drift.LocalThreadPoolJobQueue(max_workers=1)
        completed = threading.Event()
        job = {"id": "queue-contract-test", "url": "https://www.youtube.com/watch?v=test", "quality": "720p"}
        with patch.object(drift, "JOB_REPOSITORY") as repository, patch.object(drift, "run_download") as worker:
            repository.get.side_effect = [job, dict(job, status="ready")]
            repository.claim_job.return_value = job
            try:
                future = queue.enqueue(job["id"])
                future.result(timeout=2)
                self.assertEqual(repository.get.call_count, 2)
                repository.get.assert_any_call(job["id"])
                worker.assert_called_once_with(job["id"], job["url"], job["quality"])
                self.assertTrue(future.done())
            finally:
                queue.shutdown(wait=True, cancel_pending=True)

    def test_local_job_queue_rejects_unknown_job_ids(self):
        queue = drift.LocalThreadPoolJobQueue(max_workers=1)
        try:
            with patch.object(drift, "JOB_REPOSITORY") as repository:
                repository.get.return_value = None
                with self.assertRaises(ValueError):
                    queue.enqueue("missing-job")
        finally:
            queue.shutdown(wait=True, cancel_pending=True)


if __name__ == "__main__":
    unittest.main()
