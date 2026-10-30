import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from backend import main, updates, version

spec = importlib.util.spec_from_file_location("compose_updater", Path(__file__).resolve().parents[1] / "scripts/compose-updater.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


class VersionTests(unittest.IsolatedAsyncioTestCase):
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
    def test_queue_is_opt_in_and_does_not_overwrite_pending_request(self):
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"TSBOT_UPDATER_DIR": temp_dir}):
            self.assertFalse(updates.update_status()["available"])
            Path(temp_dir, "installed").touch()
            updates.submit_update("v0.1.6")
            with self.assertRaises(FileExistsError):
                updates.submit_update("v0.1.7")
            self.assertEqual({"version": "v0.1.6"}, json.loads(Path(temp_dir, "request.json").read_text()))


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

    def command(self, args, env=None, timeout=600):
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
        self.assertTrue(any("http://127.0.0.1:8080/version.json" in call for call in self.calls))

    def test_pull_failure_does_not_stop_services_or_change_selected_version(self):
        def failing(args, env=None, timeout=600):
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
        def fetch(args, env=None, timeout=600):
            recorded.append((args, timeout))
            if args[-4:] == ["pull", *worker.SERVICES]:
                raise subprocess.TimeoutExpired(args, timeout)
            return self.command(args, env, timeout)
        with patch.object(self.updater, "inspect_services", side_effect=[self.containers, new]), patch.object(self.updater, "command", side_effect=fetch):
            self.updater.run()
        primary = next((args, timeout) for args, timeout in recorded if args[-4:] == ["pull", *worker.SERVICES])
        self.assertEqual(600, primary[1])
        pulls = [(args, timeout) for args, timeout in recorded if args[:2] == ["docker", "pull"]]
        self.assertEqual([f"{worker.MIRROR_IMAGE_PREFIX}-{name}:v0.1.6" for name in worker.SERVICES], [args[-1] for args, _ in pulls])
        self.assertTrue(all(0 < timeout <= 600 for _, timeout in pulls))
        tags = [index for index, (args, _) in enumerate(recorded) if args[:3] == ["docker", "image", "tag"]]
        stop = next(index for index, (args, _) in enumerate(recorded) if "stop" in args)
        self.assertEqual(3, len(tags))
        self.assertLess(max(tags), stop)
        self.assertNotIn("ghcr.nju.edu.cn", (self.project / ".env").read_text())
        self.assertIn("南大源", json.loads((self.project / "data/updater/status.json").read_text())["message"])

    def test_mirror_failure_never_retags_partial_downloads_or_stops_services(self):
        def fetch(args, env=None, timeout=600):
            self.calls.append(args)
            if args[-4:] == ["pull", *worker.SERVICES]:
                raise subprocess.TimeoutExpired(args, timeout)
            if args[:2] == ["docker", "pull"] and "-backend:" in args[-1]:
                raise RuntimeError("mirror pull failed")
            return self.command(args, env, timeout)
        with patch.object(self.updater, "inspect_services", return_value=self.containers), patch.object(self.updater, "command", side_effect=fetch):
            with self.assertRaises(worker.ImagePullError):
                self.updater.run()
        self.assertFalse(any("stop" in args or args[:3] == ["docker", "image", "tag"] for args in self.calls))
        self.assertIn("TSBOT_IMAGE_TAG=v0.1.5", (self.project / ".env").read_text())
        self.assertFalse((self.project / "data/backups").exists())

    def test_mirror_pulls_share_one_ten_minute_budget(self):
        recorded = []
        def fetch(args, env=None, timeout=600):
            if args[-4:] == ["pull", *worker.SERVICES]:
                raise RuntimeError("GHCR failed")
            recorded.append((args, timeout))
            return ""
        with patch.object(self.updater, "command", side_effect=fetch), patch.object(worker.time, "monotonic", side_effect=[0, 0, 300, 601]):
            with self.assertRaises(worker.ImagePullError):
                self.updater.pull_images("v0.1.6", {})
        self.assertEqual([600, 300], [timeout for _, timeout in recorded])
        self.assertTrue(all(args[:2] == ["docker", "pull"] for args, _ in recorded))

    def test_primary_success_does_not_use_mirror(self):
        with patch.object(self.updater, "command") as command:
            self.assertEqual("GHCR", self.updater.pull_images("v0.1.6", {}))
        command.assert_called_once_with([*self.updater.compose, "pull", *worker.SERVICES], {}, timeout=600)

    def test_command_timeout_kills_the_compose_process_group(self):
        with patch.object(worker.subprocess, "Popen") as popen, patch.object(worker.os, "killpg") as kill_group:
            process = popen.return_value.__enter__.return_value
            process.pid = 12345
            process.communicate.side_effect = [subprocess.TimeoutExpired("docker", 600), ("", "")]
            with self.assertRaises(subprocess.TimeoutExpired):
                self.updater.command(["docker", "compose", "pull"], timeout=600)
        kill_group.assert_called_once_with(12345, worker.signal.SIGKILL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])

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
