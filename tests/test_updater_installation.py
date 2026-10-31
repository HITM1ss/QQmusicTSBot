import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from backend import updates

spec = importlib.util.spec_from_file_location("updater_installer", Path(__file__).resolve().parents[1] / "scripts/compose-updater.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class UpdaterInstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.worker = module.Updater({"project": str(self.root), "name": "test"})
        self.worker.directory.mkdir(parents=True)
        self.unit = "tsbot-updater-test"

    def tearDown(self):
        self.temp.cleanup()

    def test_enable_failure_leaves_diagnostic_but_no_ready_marker(self):
        (self.worker.directory / "installed").write_text("stale")
        def run(command, **kwargs):
            if "enable" in command:
                raise subprocess.CalledProcessError(1, command)
        with patch.object(module.shutil, "which", return_value=None), patch.object(module.subprocess, "run", side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                module.activate_systemd_updater(self.worker, self.unit, self.root)
        with patch.dict(os.environ, {"TSBOT_UPDATER_DIR": str(self.worker.directory)}):
            status = updates.update_status()
        self.assertFalse(status["available"])
        self.assertEqual("install_failed", status["state"])
        self.assertNotIn("installed", [path.name for path in self.worker.directory.iterdir()])

    def test_marker_is_created_only_after_path_is_active(self):
        observed = []
        def run(command, **kwargs):
            observed.append(command)
            self.assertFalse((self.worker.directory / "installed").exists())
        with patch.object(module.shutil, "which", return_value=None), patch.object(module.subprocess, "run", side_effect=run):
            module.activate_systemd_updater(self.worker, self.unit, self.root)
        self.assertIn(["systemctl", "is-active", "--quiet", self.unit + ".path"], observed)
        self.assertTrue((self.worker.directory / "installed").is_file())
        self.assertEqual("idle", json.loads((self.worker.directory / "status.json").read_text())["state"])

    def test_failed_active_check_does_not_advertise_updater(self):
        def run(command, **kwargs):
            if "is-active" in command:
                raise subprocess.CalledProcessError(3, command)
        with patch.object(module.shutil, "which", return_value=None), patch.object(module.subprocess, "run", side_effect=run):
            with self.assertRaises(subprocess.CalledProcessError):
                module.activate_systemd_updater(self.worker, self.unit, self.root)
        self.assertFalse((self.worker.directory / "installed").exists())

    def test_service_leaves_time_for_primary_mirror_and_recreation(self):
        service, _ = module.systemd_unit_contents(self.root, "test", self.root / "worker.py", self.root / "config.json")
        self.assertIn("TimeoutStartSec=60min", service)
        self.assertLess(module.PULL_TIMEOUT_SECONDS + module.MIRROR_PULL_TIMEOUT_SECONDS, 60 * 60)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "requires Linux systemd parser")
    def test_generated_units_pass_real_systemd_parser(self):
        # Path directives keep the entire RHS as a path, including spaces, without shell quotes.
        project = self.root / "deployment with spaces"
        for state in ("", "quoted"):
            service, path = module.systemd_unit_contents(project, "test", self.root / "worker.py", self.root / "config.json")
            if state == "quoted":
                value = str(project / "data/updater/request.json")
                path = path.replace(f"PathExists={value}", f'PathExists="{value}"')
            (self.root / (self.unit + ".service")).write_text(service)
            (self.root / (self.unit + ".path")).write_text(path)
            (self.root / "docker.service").write_text("[Service]\nType=oneshot\nExecStart=/bin/true\nRemainAfterExit=yes\n")
            result = subprocess.run(["systemd-analyze", "verify", str(self.root / (self.unit + ".path")), str(self.root / (self.unit + ".service"))],
                                    env={**os.environ, "SYSTEMD_UNIT_PATH": str(self.root) + ":"}, capture_output=True, text=True)
            if state == "quoted":
                self.assertNotEqual(0, result.returncode)
            else:
                self.assertEqual(0, result.returncode, result.stderr)
