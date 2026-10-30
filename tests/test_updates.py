import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from backend import main, updates, version

spec = importlib.util.spec_from_file_location("compose_updater", Path(__file__).resolve().parents[1] / "scripts/compose-updater.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class VersionTests(unittest.IsolatedAsyncioTestCase):
    def test_update_source_is_limited_to_the_two_supported_registries(self):
        self.assertEqual("ghcr", main.SoftwareUpdateRequest(version="v0.1.9").image_source)
        for source in ("ghcr", "nju"):
            self.assertEqual(source, main.SoftwareUpdateRequest(version="v0.1.9", image_source=source).image_source)
        with self.assertRaises(ValidationError):
            main.SoftwareUpdateRequest(version="v0.1.9", image_source="https://example.test")

    async def test_selected_source_reaches_the_host_request_and_pending_status(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TSBOT_UPDATER_DIR": directory}):
            Path(directory, "installed").touch()
            with (
                patch.object(main, "_require_admin_token"),
                patch.object(main, "latest_release", AsyncMock(return_value={"version": "v0.1.9"})),
                patch.object(main, "build_info", return_value={"version": "v0.1.8"}),
            ):
                result = await main.admin_start_update(main.SoftwareUpdateRequest(version="v0.1.9", image_source="nju"), object())
            self.assertEqual("nju", result["image_source"])
            self.assertEqual({"version": "v0.1.9", "image_source": "nju"}, json.loads(Path(directory, "request.json").read_text()))
            status = updates.update_status()
            self.assertEqual("pending", status["state"])
            self.assertEqual("nju", status["image_source"])
            self.assertEqual("v0.1.9", status["version"])

    def test_versions_compare_numerically(self):
        self.assertGreater(version.parse_version("v0.1.10"), version.parse_version("v0.1.9"))
        for value in ("development", "v1.0.0-rc1", "v01.0.0", "v1.0.0; touch /tmp/test"):
            self.assertIsNone(version.parse_version(value))

    async def test_changed_release_does_not_queue_an_unconfirmed_version(self):
        with (
            patch.object(main, "_require_admin_token"),
            patch.object(main, "update_status", return_value={"available": True}),
            patch.object(main, "latest_release", AsyncMock(return_value={"version": "v0.1.7"})),
            patch.object(main, "build_info", return_value={"version": "v0.1.5"}),
            patch.object(main, "submit_update") as submit,
        ):
            with self.assertRaises(main.HTTPException) as raised:
                await main.admin_start_update(main.SoftwareUpdateRequest(version="v0.1.6"), object())
        self.assertEqual(409, raised.exception.status_code)
        submit.assert_not_called()

    async def test_up_to_date_or_development_backend_is_not_replaced(self):
        for current in ("v0.1.7", "development", "v0.1.8"):
            with (
                patch.object(main, "_require_admin_token"),
                patch.object(main, "update_status", return_value={"available": True}),
                patch.object(main, "latest_release", AsyncMock(return_value={"version": "v0.1.7"})),
                patch.object(main, "build_info", return_value={"version": current}),
                patch.object(main, "submit_update") as submit,
            ):
                with self.assertRaises(main.HTTPException):
                    await main.admin_start_update(main.SoftwareUpdateRequest(version="v0.1.7"), object())
                submit.assert_not_called()

    async def test_update_requires_admin_before_checking_network_or_files(self):
        with patch.object(main, "_require_admin_token", side_effect=main.HTTPException(403)), patch.object(main, "latest_release") as latest:
            with self.assertRaises(main.HTTPException):
                await main.admin_start_update(main.SoftwareUpdateRequest(version="v0.1.6"), object())
        latest.assert_not_called()


class UpdateQueueTests(unittest.TestCase):
    def test_status_passes_through_pull_progress_and_clears_it_for_other_stages(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"TSBOT_UPDATER_DIR": temp_dir}):
            directory = Path(temp_dir)
            (directory / "installed").touch()
            updater = worker.Updater({"project": str(directory), "name": "test"})
            updater.directory = directory
            updater.status("running", "正在拉取更新镜像", "v0.1.8", progress=23)
            self.assertEqual(23, updates.update_status()["progress"])
            self.assertEqual("v0.1.8", updates.update_status()["version"])
            updater.status("running", "正在备份数据库", "v0.1.8")
            self.assertIsNone(updates.update_status()["progress"])

    def test_queue_is_opt_in_and_does_not_overwrite_pending_request(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"TSBOT_UPDATER_DIR": temp_dir}):
            self.assertFalse(updates.update_status()["available"])
            Path(temp_dir, "installed").touch()
            updates.submit_update("v0.1.6")
            with self.assertRaises(FileExistsError):
                updates.submit_update("v0.1.7")
            self.assertEqual({"version": "v0.1.6", "image_source": "ghcr"}, json.loads(Path(temp_dir, "request.json").read_text()))

    def test_legacy_pending_requests_default_to_the_original_registry(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TSBOT_UPDATER_DIR": directory}):
            Path(directory, "installed").touch()
            Path(directory, "request.json").write_text('{"version":"v0.1.8"}')
            status = updates.update_status()
            self.assertEqual("pending", status["state"])
            self.assertEqual("ghcr", status["image_source"])

    def test_selected_source_remains_visible_in_each_update_stage(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TSBOT_UPDATER_DIR": directory}):
            root = Path(directory)
            (root / "installed").touch()
            updater = worker.Updater({"project": directory, "name": "test"})
            updater.directory = root
            updater.image_source = "nju"
            for state in ("running", "success", "failed"):
                updater.status(state, "test", "v0.1.9")
                status = updates.update_status()
                self.assertEqual(state, status["state"])
                self.assertEqual("nju", status["image_source"])


class PullProgressTests(unittest.TestCase):
    def test_interleaved_layers_are_weighted_by_bytes_without_double_counting(self):
        progress = worker.PullProgress()
        first = {"parent_id": "backend", "id": "first", "text": "Downloading", "current": 25, "total": 100}
        self.assertEqual(8, progress.update(first))
        self.assertEqual(8, progress.update(first))
        second = {"parent_id": "backend", "id": "second", "text": "Downloading", "current": 225, "total": 900}
        self.assertEqual(8, progress.update(second))
        self.assertEqual(25, progress.update({"parent_id": "web", "id": "first", "text": "Downloading", "current": 50, "total": 100}))
        # Decompression sizes must not replace the original compressed download sizes.
        self.assertEqual(47, progress.update({"parent_id": "backend", "id": "second", "text": "Extracting", "current": 1, "total": 90000}))

    def test_cached_images_and_current_compose_image_ids_count_as_completed(self):
        progress = worker.PullProgress()
        for index, service in enumerate(worker.SERVICES, 1):
            identifier = f"Image {worker.IMAGE_PREFIX}-{service}:v0.1.8"
            self.assertEqual(min(99, index * 100 // 3), progress.update({"id": identifier, "text": "Pulled"}))
        self.assertEqual(99, progress.update({"id": identifier, "text": "Pulled"}))


class HostUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name)
        (self.project / "data/updater").mkdir(parents=True)
        self.updater = worker.Updater({"project": str(self.project), "name": "tsbot-test"})
        self.calls = []
        self.containers = {name: {"Id": name, "Config": {"Image": f"{worker.IMAGE_PREFIX}-{name}:v0.1.5", "Env": ["TSBOT_BUILD_VERSION=v0.1.5"]}} for name in worker.SERVICES}
        with sqlite3.connect(self.project / "data/tsbot.db") as db:
            db.execute("CREATE TABLE preserved (value TEXT)")
            db.execute("INSERT INTO preserved VALUES ('saved-login')")
        (self.project / ".env").write_text("# existing settings\nKEEP=this\nTSBOT_IMAGE_TAG=v0.1.5\n")
        (self.project / "data/updater/request.json").write_text('{"version":"v0.1.6"}')

    def tearDown(self):
        self.temp.cleanup()

    def command(self, args, env=None, timeout=600, on_line=None):
        self.calls.append(args)
        if "config" in args:
            return json.dumps({"services": {name: {"image": f"{worker.IMAGE_PREFIX}-{name}:v0.1.6"} for name in worker.SERVICES}})
        if args[1:3] == ["exec", "web"]:
            return '{"version":"v0.1.6"}'
        return ""

    def test_success_preserves_settings_and_database_and_checks_frontend(self):
        new = {name: {"Id": name, "Config": {"Image": f"{worker.IMAGE_PREFIX}-{name}:v0.1.6", "Env": ["TSBOT_BUILD_VERSION=v0.1.6"]}} for name in worker.SERVICES}
        with patch.object(self.updater, "inspect_services", side_effect=[self.containers, new]), patch.object(self.updater, "command", side_effect=self.command):
            self.updater.run()
        self.assertIn("KEEP=this", (self.project / ".env").read_text())
        self.assertIn("TSBOT_IMAGE_TAG=v0.1.6", (self.project / ".env").read_text())
        backups = list((self.project / "data/backups").glob("*.db"))
        self.assertEqual(1, len(backups))
        with sqlite3.connect(backups[0]) as db:
            self.assertEqual("saved-login", db.execute("SELECT value FROM preserved").fetchone()[0])
        status = json.loads((self.project / "data/updater/status.json").read_text())
        self.assertEqual("success", status["state"])
        self.assertEqual("ghcr", status["image_source"])
        self.assertTrue(any("http://127.0.0.1:8080/version.json" in call for call in self.calls))

    def test_pull_failure_does_not_stop_services_or_change_selected_version(self):
        def failing(args, env=None, timeout=600, on_line=None):
            if "pull" in args:
                raise RuntimeError("pull failed")
            return self.command(args, env, timeout)
        with patch.object(self.updater, "inspect_services", return_value=self.containers), patch.object(self.updater, "command", side_effect=failing):
            with self.assertRaises(RuntimeError):
                self.updater.run()
        self.assertFalse(any("stop" in call for call in self.calls))
        self.assertIn("TSBOT_IMAGE_TAG=v0.1.5", (self.project / ".env").read_text())
        self.assertEqual("failed", json.loads((self.project / "data/updater/status.json").read_text())["state"])

    def test_primary_timeout_switches_to_mirror_and_retags_before_stopping(self):
        new = {name: {"Id": name, "Config": {"Image": f"{worker.IMAGE_PREFIX}-{name}:v0.1.6", "Env": ["TSBOT_BUILD_VERSION=v0.1.6"]}} for name in worker.SERVICES}
        recorded = []
        def fetch(args, env=None, timeout=600, on_line=None):
            recorded.append((args, env, timeout))
            if "pull" in args and env.get("TSBOT_IMAGE_REGISTRY") != "ghcr.nju.edu.cn":
                raise subprocess.TimeoutExpired(args, timeout)
            return self.command(args, env, timeout)
        with patch.object(self.updater, "inspect_services", side_effect=[self.containers, new]), patch.object(self.updater, "command", side_effect=fetch):
            self.updater.run()
        pulls = [(args, env, timeout) for args, env, timeout in recorded if "pull" in args]
        self.assertEqual(2, len(pulls))
        self.assertTrue(all(args[-4:] == ["pull", *worker.SERVICES] and timeout == 600 for args, _, timeout in pulls))
        self.assertEqual("ghcr.nju.edu.cn", pulls[1][1]["TSBOT_IMAGE_REGISTRY"])
        self.assertEqual("v0.1.6", pulls[1][1]["TSBOT_IMAGE_TAG"])
        tags = [index for index, (args, _, _) in enumerate(recorded) if args[:3] == ["docker", "image", "tag"]]
        stop = next(index for index, (args, _, _) in enumerate(recorded) if "stop" in args)
        self.assertEqual(3, len(tags))
        self.assertLess(max(tags), stop)
        self.assertNotIn("ghcr.nju.edu.cn", (self.project / ".env").read_text())
        self.assertIn("南大源", json.loads((self.project / "data/updater/status.json").read_text())["message"])
        self.assertEqual("ghcr", json.loads((self.project / "data/updater/status.json").read_text())["image_source"])

    def test_selected_mirror_skips_ghcr_and_retags_before_recreating(self):
        (self.updater.directory / "request.json").write_text('{"version":"v0.1.6","image_source":"nju"}')
        new = {name: {"Id": name, "Config": {"Image": f"{worker.IMAGE_PREFIX}-{name}:v0.1.6", "Env": ["TSBOT_BUILD_VERSION=v0.1.6"]}} for name in worker.SERVICES}
        pulls = []
        def command(args, env=None, timeout=600, on_line=None):
            if "pull" in args:
                pulls.append(env)
            return self.command(args, env, timeout, on_line)
        with patch.object(self.updater, "inspect_services", side_effect=[self.containers, new]), patch.object(self.updater, "command", side_effect=command):
            self.updater.run()
        self.assertEqual(1, len(pulls))
        self.assertEqual("ghcr.nju.edu.cn", pulls[0]["TSBOT_IMAGE_REGISTRY"])
        tags = [args for args in self.calls if args[:3] == ["docker", "image", "tag"]]
        self.assertEqual([[f"{worker.MIRROR_IMAGE_PREFIX}-{name}:v0.1.6", f"{worker.IMAGE_PREFIX}-{name}:v0.1.6"] for name in worker.SERVICES], [args[-2:] for args in tags])
        self.assertLess(max(self.calls.index(args) for args in tags), next(i for i, args in enumerate(self.calls) if "stop" in args))
        self.assertNotIn("ghcr.nju.edu.cn", (self.project / ".env").read_text())
        status = json.loads((self.updater.directory / "status.json").read_text())
        self.assertEqual("success", status["state"])
        self.assertEqual("nju", status["image_source"])

    def test_selected_mirror_failure_keeps_services_and_version(self):
        (self.updater.directory / "request.json").write_text('{"version":"v0.1.6","image_source":"nju"}')
        pulls = []
        def command(args, env=None, timeout=600, on_line=None):
            if "pull" in args:
                pulls.append(env)
                raise RuntimeError("mirror failed")
            return self.command(args, env, timeout, on_line)
        with patch.object(self.updater, "inspect_services", return_value=self.containers), patch.object(self.updater, "command", side_effect=command):
            with self.assertRaisesRegex(worker.ImagePullError, "南大源拉取失败"):
                self.updater.run()
        self.assertEqual(1, len(pulls))
        self.assertEqual("ghcr.nju.edu.cn", pulls[0]["TSBOT_IMAGE_REGISTRY"])
        self.assertFalse(any("stop" in args or args[:3] == ["docker", "image", "tag"] for args in self.calls))
        self.assertIn("TSBOT_IMAGE_TAG=v0.1.5", (self.project / ".env").read_text())
        status = json.loads((self.updater.directory / "status.json").read_text())
        self.assertEqual("nju", status["image_source"])
        self.assertNotIn("GHCR", status["message"])

    def test_finalize_preserves_the_selected_source_after_interruption(self):
        config = self.project / "config.json"
        config.write_text(json.dumps({"project": str(self.project), "name": "test"}))
        self.updater.image_source = "nju"
        self.updater.status("running", "正在从南大源拉取更新镜像", "v0.1.6", progress=23)
        subprocess.run([sys.executable, str(Path(worker.__file__)), str(config), "--finalize"], check=True, capture_output=True)
        status = json.loads((self.updater.directory / "status.json").read_text())
        self.assertEqual("failed", status["state"])
        self.assertEqual("nju", status["image_source"])
        self.assertIsNone(status["progress"])
        self.assertFalse((self.updater.directory / "request.json").exists())

    def test_mirror_failure_never_retags_partial_downloads_or_stops_services(self):
        def fetch(args, env=None, timeout=600, on_line=None):
            self.calls.append(args)
            if "pull" in args and env.get("TSBOT_IMAGE_REGISTRY") != "ghcr.nju.edu.cn":
                raise subprocess.TimeoutExpired(args, timeout)
            if "pull" in args:
                raise RuntimeError("mirror pull failed")
            return self.command(args, env, timeout)
        with patch.object(self.updater, "inspect_services", return_value=self.containers), patch.object(self.updater, "command", side_effect=fetch):
            with self.assertRaises(worker.ImagePullError):
                self.updater.run()
        self.assertFalse(any("stop" in args or args[:3] == ["docker", "image", "tag"] for args in self.calls))
        self.assertIn("TSBOT_IMAGE_TAG=v0.1.5", (self.project / ".env").read_text())
        self.assertFalse((self.project / "data/backups").exists())

    def test_each_registry_has_one_shared_ten_minute_pull_budget(self):
        with patch.object(self.updater, "command", side_effect=[RuntimeError("GHCR failed"), subprocess.TimeoutExpired("mirror", 600)]) as command:
            with self.assertRaises(worker.ImagePullError):
                self.updater.pull_images("v0.1.6", {})
        self.assertEqual(2, command.call_count)
        for call in command.call_args_list:
            self.assertEqual(["pull", *worker.SERVICES], call.args[0][-4:])
            self.assertEqual(600, call.kwargs["timeout"])

    def test_primary_success_does_not_use_mirror(self):
        with patch.object(self.updater, "command") as command:
            self.assertEqual("GHCR", self.updater.pull_images("v0.1.6", {}))
        self.assertEqual(1, command.call_count)
        self.assertEqual([*self.updater.compose, "--progress", "json", "pull", *worker.SERVICES], command.call_args.args[0])
        self.assertEqual({}, command.call_args.args[1])
        self.assertEqual(600, command.call_args.kwargs["timeout"])

    def test_command_timeout_kills_the_compose_process_group(self):
        with patch.object(worker.subprocess, "Popen") as popen, patch.object(worker.os, "killpg") as kill_group:
            process = popen.return_value.__enter__.return_value
            process.pid = 12345
            process.communicate.side_effect = [subprocess.TimeoutExpired("docker", 600), ("", "")]
            with self.assertRaises(subprocess.TimeoutExpired):
                self.updater.command(["docker", "compose", "pull"], timeout=600)
        kill_group.assert_called_once_with(12345, worker.signal.SIGKILL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

    def test_pull_reports_live_progress_and_only_finishes_when_command_succeeds(self):
        def command(args, env=None, timeout=600, on_line=None):
            on_line("time=now level=warning msg=compose-warning")
            on_line(json.dumps({"parent_id": "backend", "id": "layer", "text": "Downloading", "current": 69, "total": 100}))
            self.assertEqual(23, json.loads((self.updater.directory / "status.json").read_text())["progress"])
            for service in worker.SERVICES:
                on_line(json.dumps({"id": service, "text": "Pulled"}))
            self.assertEqual(99, json.loads((self.updater.directory / "status.json").read_text())["progress"])
            return ""
        with patch.object(self.updater, "command", side_effect=command):
            self.updater.pull_with_progress("v0.1.8", {})
        status = json.loads((self.updater.directory / "status.json").read_text())
        self.assertEqual(100, status["progress"])
        self.assertEqual("正在拉取更新镜像", status["message"])
        self.assertEqual("v0.1.8", status["version"])

    def test_pulled_events_followed_by_command_failure_do_not_report_one_hundred(self):
        def command(args, env=None, timeout=600, on_line=None):
            for service in worker.SERVICES:
                on_line(json.dumps({"id": service, "text": "Pulled"}))
            raise RuntimeError("image inspection failed")
        with patch.object(self.updater, "command", side_effect=command):
            with self.assertRaises(RuntimeError):
                self.updater.pull_with_progress("v0.1.8", {})
        self.assertEqual(99, json.loads((self.updater.directory / "status.json").read_text())["progress"])

    def test_switching_registry_starts_a_new_progress_count(self):
        starts = []
        def command(args, env=None, timeout=600, on_line=None):
            if "pull" not in args:
                return ""
            starts.append(json.loads((self.updater.directory / "status.json").read_text()))
            on_line(json.dumps({"parent_id": "backend", "id": "layer", "text": "Downloading", "current": 69, "total": 100}))
            if env.get("TSBOT_IMAGE_REGISTRY") != "ghcr.nju.edu.cn":
                raise RuntimeError("GHCR failed")
            return ""
        with patch.object(self.updater, "command", side_effect=command):
            self.assertEqual("南大源", self.updater.pull_images("v0.1.8", {}))
        self.assertEqual([0, 0], [status["progress"] for status in starts])
        self.assertIn("南大源", starts[1]["message"])

    def test_stream_reads_split_lines_from_stdout_and_stderr(self):
        lines = []
        code = "import os, time; os.write(1, b'{\"current\":'); time.sleep(0.02); os.write(1, b'23}\\n'); os.write(2, b'{\"current\":42}\\n')"
        self.updater.command([sys.executable, "-c", code], timeout=3, on_line=lines.append)
        self.assertEqual([{"current": 23}, {"current": 42}], [json.loads(line) for line in lines])

    def test_stream_timeout_still_kills_process_when_silent_or_line_is_incomplete(self):
        for prefix in ("", "import os; os.write(2, b'{\"current\":'); "):
            with patch.object(worker.os, "killpg", wraps=os.killpg) as kill_group:
                with self.assertRaises(subprocess.TimeoutExpired):
                    self.updater.command([sys.executable, "-c", prefix + "import time; time.sleep(5)"], timeout=0.1, on_line=lambda line: None)
            self.assertEqual(worker.signal.SIGKILL, kill_group.call_args.args[1])

    def test_missing_database_restarts_old_backend_and_keeps_old_tag(self):
        (self.project / "data/tsbot.db").unlink()
        with patch.object(self.updater, "inspect_services", return_value=self.containers), patch.object(self.updater, "command", side_effect=self.command):
            with self.assertRaises(RuntimeError):
                self.updater.run()
        self.assertTrue(any(call[-2:] == ["start", "backend"] for call in self.calls))
        self.assertFalse(any("up" in call for call in self.calls))
        self.assertIn("TSBOT_IMAGE_TAG=v0.1.5", (self.project / ".env").read_text())

    def test_wrong_running_version_is_reported_as_failure(self):
        with (
            patch.object(self.updater, "inspect_services", return_value=self.containers),
            patch.object(self.updater, "command", side_effect=self.command),
            patch.object(worker.time, "sleep"),
        ):
            with self.assertRaises(RuntimeError):
                self.updater.run()
        status = json.loads((self.project / "data/updater/status.json").read_text())
        self.assertEqual("failed", status["state"])
        self.assertIn("data/backups", status["message"])
