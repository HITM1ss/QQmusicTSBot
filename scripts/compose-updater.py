#!/usr/bin/env python3
"""Host-side worker for the opt-in systemd Compose updater. Python standard library only."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

SERVICES = ("voice-service", "backend", "web")
IMAGE_PREFIX = "ghcr.io/hitm1ss/qqmusictsbot"


def systemd_unit_contents(project: Path, name: str, worker_path: Path, config_path: Path) -> tuple[str, str]:
    unit = f"tsbot-updater-{name}"
    service = f'''[Unit]
Description=TSBot Compose updater ({name})
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {worker_path} {config_path}
ExecStopPost=/usr/bin/python3 {worker_path} {config_path} --finalize
TimeoutStartSec=20min
UMask=0077
'''
    path = f'''[Unit]
Description=Watch TSBot update requests ({name})

[Path]
PathExists={project / 'data/updater/request.json'}
Unit={unit}.service

[Install]
WantedBy=multi-user.target
'''
    return service, path


def activate_systemd_updater(worker, unit: str, units: Path) -> None:
    marker = worker.directory / "installed"
    marker.unlink(missing_ok=True)
    worker.status("installing", "正在安装宿主机更新服务")
    try:
        if shutil.which("systemd-analyze"):
            subprocess.run(["systemd-analyze", "verify", str(units / f"{unit}.service"), str(units / f"{unit}.path")], check=True)
        subprocess.run(["systemctl", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "enable", "--now", f"{unit}.path"], check=True)
        subprocess.run(["systemctl", "is-active", "--quiet", f"{unit}.path"], check=True)
        worker.status("idle", "更新服务已启用")
        atomic_write(marker, unit + "\n", 0o644)
    except (subprocess.CalledProcessError, OSError):
        marker.unlink(missing_ok=True)
        worker.status("install_failed", f"更新服务安装或启动失败，请查看 {unit}.path 的 systemd 日志后重新安装")
        raise


def version_tuple(value: str) -> tuple[int, ...]:
    if not re.fullmatch(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", value):
        raise ValueError("Invalid release version")
    return tuple(map(int, value[1:].split(".")))


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".updater-")
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def set_image_tag(content: str, version: str) -> str:
    version_tuple(version)
    lines = [line for line in content.splitlines() if not re.match(r"^\s*(?:export\s+)?TSBOT_IMAGE_TAG\s*=", line)]
    return "\n".join([*lines, f"TSBOT_IMAGE_TAG={version}", ""])


class Updater:
    def __init__(self, config: dict):
        self.project = Path(config["project"])
        self.directory = self.project / "data" / "updater"
        self.compose = ["docker", "compose", "--project-directory", str(self.project), "--project-name", config["name"], "-f", str(self.project / "docker-compose.prebuilt.yml")]

    def command(self, args: list[str], env: dict | None = None, timeout: int = 600) -> str:
        result = subprocess.run(args, cwd=self.project, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
        if result.returncode:
            # Compose config and container inspect can contain credentials. Never dump their output.
            raise RuntimeError(f"Command failed: {args[0]} (exit {result.returncode})")
        return result.stdout

    def status(self, state: str, message: str, version: str = "") -> None:
        atomic_write(self.directory / "status.json", json.dumps({"state": state, "message": message, "version": version}, ensure_ascii=False), 0o644)
        print(f"{state}: {message}", flush=True)

    def inspect_services(self) -> dict[str, dict]:
        containers = {}
        for service in SERVICES:
            identifier = self.command([*self.compose, "ps", "-q", service]).strip()
            if not identifier or "\n" in identifier:
                raise RuntimeError(f"Expected one running container for {service}")
            container = json.loads(self.command(["docker", "inspect", identifier]))[0]
            if not container["State"]["Running"] or not container["Config"]["Image"].startswith(f"{IMAGE_PREFIX}-{service}:"):
                raise RuntimeError("Updater requires this repository's GHCR prebuilt deployment")
            if service == "backend":
                environment = dict(item.split("=", 1) for item in container["Config"]["Env"] if "=" in item)
                data_mount = next((mount for mount in container["Mounts"] if mount["Destination"] == "/app/data"), None)
                if environment.get("DATABASE_URL") != "sqlite:///./data/tsbot.db" or not data_mount or Path(data_mount["Source"]).resolve() != (self.project / "data").resolve():
                    raise RuntimeError("Updater requires the standard data/tsbot.db bind mount")
            containers[service] = container
        return containers

    def run(self) -> None:
        with (self.directory / ".lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            request = self.directory / "request.json"
            if not request.exists():
                return
            version = ""
            stopped = False
            previous_env: str | None = None
            env_file = self.project / ".env"
            tag_written = False
            try:
                payload = json.loads(request.read_text(encoding="utf-8"))
                version = payload["version"]
                target = version_tuple(version)
                self.status("running", "正在检查部署", version)
                containers = self.inspect_services()
                backend_env = dict(item.split("=", 1) for item in containers["backend"]["Config"]["Env"] if "=" in item)
                if target <= version_tuple(backend_env.get("TSBOT_BUILD_VERSION", "")):
                    raise ValueError("Target must be newer than the running backend")
                environment = {**os.environ, "TSBOT_IMAGE_TAG": version}
                resolved = json.loads(self.command([*self.compose, "config", "--format", "json"], environment))
                for service in SERVICES:
                    if resolved["services"][service]["image"] != f"{IMAGE_PREFIX}-{service}:{version}":
                        raise ValueError("Unexpected image repository in Compose configuration")
                self.status("running", "正在拉取更新镜像", version)
                self.command([*self.compose, "pull", *SERVICES], environment)
                # Stop writes before making the upgrade backup.
                self.status("running", "正在备份数据库", version)
                self.command([*self.compose, "stop", "backend"])
                stopped = True
                database = self.project / "data" / "tsbot.db"
                if not database.is_file():
                    raise RuntimeError("Expected data/tsbot.db was not found")
                backup_dir = self.project / "data" / "backups"
                backup_dir.mkdir(mode=0o700, exist_ok=True)
                backup_path = backup_dir / f"tsbot-before-{version}-{time.time_ns()}.db"
                with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source, sqlite3.connect(backup_path) as backup:
                    source.backup(backup)
                backup_path.chmod(0o600)
                previous_env = env_file.read_text(encoding="utf-8") if env_file.exists() else None
                env_stat = env_file.stat() if env_file.exists() else None
                atomic_write(env_file, set_image_tag(previous_env or "", version), env_stat.st_mode & 0o777 if env_stat else 0o644)
                tag_written = True
                if env_stat:
                    os.chown(env_file, env_stat.st_uid, env_stat.st_gid)
                self.status("running", "正在重建服务，网页会暂时断开", version)
                self.command([*self.compose, "up", "-d", "--no-build", "--pull", "never", "--force-recreate", *SERVICES], environment)
                stopped = False
                self.status("running", "正在检查服务", version)
                for attempt in range(30):
                    try:
                        updated = self.inspect_services()
                        if any(updated[name]["Config"]["Image"] != f"{IMAGE_PREFIX}-{name}:{version}" for name in SERVICES):
                            raise RuntimeError("A service is still using an earlier image")
                        backend_id = updated["backend"]["Id"]
                        web_id = updated["web"]["Id"]
                        running_env = dict(item.split("=", 1) for item in updated["backend"]["Config"]["Env"] if "=" in item)
                        if running_env.get("TSBOT_BUILD_VERSION") != version:
                            raise RuntimeError("Backend version does not match")
                        self.command(["docker", "exec", backend_id, "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8009/auth/status', timeout=5).read()"], timeout=10)
                        served = json.loads(self.command(["docker", "exec", web_id, "wget", "-qO-", "http://127.0.0.1:8080/version.json"], timeout=10))
                        if served["version"] != version:
                            raise RuntimeError("Frontend version does not match")
                        self.status("success", "更新完成，请刷新页面", version)
                        return
                    except (RuntimeError, subprocess.TimeoutExpired, ValueError, KeyError):
                        if attempt == 29:
                            raise RuntimeError("Updated services did not pass readiness checks")
                        time.sleep(2)
            except Exception as exc:
                if stopped:
                    # Only the old backend was stopped: restart it before reporting failure.
                    try:
                        self.command([*self.compose, "start", "backend"])
                    except Exception:
                        pass
                if tag_written:
                    # Keep the selected tag for diagnosis; a failed startup may already have migrated data.
                    message = "更新后服务检查失败，请查看宿主机日志；数据库备份位于 data/backups"
                else:
                    message = "更新失败，未切换版本，请查看宿主机更新服务日志"
                self.status("failed", message, version)
                print(f"Failure: {type(exc).__name__}: {exc}", file=sys.stderr)
                raise
            finally:
                request.unlink(missing_ok=True)


if __name__ == "__main__":
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    updater = Updater(config)
    if "--finalize" in sys.argv[2:]:
        try:
            state = json.loads((updater.directory / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("state") == "running":
            (updater.directory / "request.json").unlink(missing_ok=True)
            updater.status("failed", "更新服务异常中断，请查看宿主机日志", state.get("version", ""))
    else:
        updater.run()
