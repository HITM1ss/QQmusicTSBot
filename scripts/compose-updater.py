#!/usr/bin/env python3
"""Host-side worker for the opt-in systemd Compose updater. Python standard library only."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

SERVICES = ("voice-service", "backend", "web")
IMAGE_PREFIX = "ghcr.io/hitm1ss/qqmusictsbot"
MIRROR_IMAGE_PREFIX = "ghcr.nju.edu.cn/hitm1ss/qqmusictsbot"
PULL_TIMEOUT_SECONDS = 600
NTP_SERVER = "ntp.aliyun.com"
NTP_UNIX_OFFSET = 2_208_988_800


def network_time() -> float:
    """Fetch one SNTP sample, accounting for the round trip and server processing."""
    packet = bytearray(48)
    packet[0] = 0x23  # NTP v4, client mode.
    sent_time = time.time() + NTP_UNIX_OFFSET
    packet[40:48] = struct.pack("!II", int(sent_time), int((sent_time % 1) * 2**32))
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        connection.settimeout(5)
        connection.connect((NTP_SERVER, 123))
        started = time.monotonic()
        connection.send(packet)
        response = connection.recv(48)
        elapsed = time.monotonic() - started
    if len(response) != 48 or response[0] & 7 != 4 or response[0] >> 6 == 3 or not 1 <= response[1] <= 15:
        raise ValueError("NTP 服务未返回有效的已同步时间")
    if response[24:32] != packet[40:48]:
        raise ValueError("NTP 响应与本次请求不匹配")
    received_seconds, received_fraction, sent_seconds, sent_fraction = struct.unpack("!IIII", response[32:48])
    if not received_seconds or not sent_seconds:
        raise ValueError("NTP 时间戳为空")
    received = received_seconds - NTP_UNIX_OFFSET + received_fraction / 2**32
    sent = sent_seconds - NTP_UNIX_OFFSET + sent_fraction / 2**32
    return (received + sent + elapsed) / 2


def sync_host_time() -> str:
    timestamp = network_time()
    time.clock_settime(time.CLOCK_REALTIME, timestamp)
    return datetime.fromtimestamp(timestamp, timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


class ImagePullError(RuntimeError):
    """Both configured registries failed before any service was stopped."""


class PullProgress:
    def __init__(self):
        self.layers: dict[str, dict[str, tuple[int, int]]] = {}
        self.completed: set[str] = set()

    def update(self, event: dict) -> int:
        parent = event.get("parent_id")
        phase = event.get("text")
        if parent and phase == "Downloading" and event.get("total", 0) > 0:
            self.layers.setdefault(parent, {})[event["id"]] = (event.get("current", 0), event["total"])
        elif parent and phase in {"Download complete", "Verifying Checksum", "Extracting", "Pull complete"}:
            layers = self.layers.get(parent, {})
            if event["id"] in layers:
                _, total = layers[event["id"]]
                layers[event["id"]] = (total, total)
        elif not parent and phase == "Pulled":
            self.completed.add(event["id"])
        downloaded = float(len(self.completed))
        for image, layers in self.layers.items():
            if image not in self.completed:
                downloaded += sum(current for current, _ in layers.values()) / sum(total for _, total in layers.values())
        # Each image contributes one third. Only a successful pull command reaches 100%.
        return min(99, int(downloaded * 100 / len(SERVICES)))


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
TimeoutStartSec=35min
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
        self.image_source = "ghcr"
        self.compose = ["docker", "compose", "--project-directory", str(self.project), "--project-name", config["name"], "-f", str(self.project / "docker-compose.prebuilt.yml")]

    def command(self, args: list[str], env: dict | None = None, timeout: float = 600, on_line: Callable[[str], None] | None = None) -> str:
        with subprocess.Popen(args, cwd=self.project, env=env, text=on_line is None, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT if on_line else subprocess.PIPE, start_new_session=True) as process:
            try:
                if on_line is None:
                    output, _ = process.communicate(timeout=timeout)
                else:
                    deadline = time.monotonic() + timeout
                    pending = b""
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ)
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0 or not selector.select(remaining):
                                raise subprocess.TimeoutExpired(args, timeout)
                            chunk = os.read(process.stdout.fileno(), 65536)
                            if not chunk:
                                break
                            pending += chunk
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                on_line(line.decode("utf-8"))
                    process.wait(timeout=deadline - time.monotonic())
                    output = ""
            except subprocess.TimeoutExpired:
                # Compose can launch a CLI plugin child. Stop the whole client process group
                # before trying another registry, rather than leaving an old pull running.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.communicate()
                raise
        if process.returncode:
            # Compose config and container inspect can contain credentials. Never dump their output.
            raise RuntimeError(f"Command failed: {args[0]} (exit {process.returncode})")
        return output

    def pull_with_progress(self, version: str, environment: dict, message: str = "正在拉取更新镜像") -> None:
        progress = PullProgress()
        previous = 0
        self.status("running", message, version, progress=0)

        def on_line(line: str) -> None:
            nonlocal previous
            # Compose can also emit plain-text warnings on its progress stream.
            if not line.startswith("{"):
                return
            percent = progress.update(json.loads(line))
            if percent != previous:
                self.status("running", message, version, progress=percent)
                previous = percent

        self.command([*self.compose, "--progress", "json", "pull", *SERVICES], environment,
                     timeout=PULL_TIMEOUT_SECONDS, on_line=on_line)
        self.status("running", message, version, progress=100)

    def pull_images(self, version: str, environment: dict) -> str:
        registry = {"ghcr": "ghcr.io", "nju": "ghcr.nju.edu.cn"}[self.image_source]
        message = "正在从南大源拉取更新镜像"
        if registry == "ghcr.io":
            try:
                self.pull_with_progress(version, environment)
                return "GHCR"
            except (RuntimeError, subprocess.TimeoutExpired) as exc:
                reason = "超时（10 分钟）" if isinstance(exc, subprocess.TimeoutExpired) else "失败"
                registry = "ghcr.nju.edu.cn"
                message = f"GHCR 拉取{reason}，正在从南大源拉取更新镜像"
        try:
            self.pull_with_progress(version, {**environment, "TSBOT_IMAGE_REGISTRY": registry}, message)
            # Keep Compose and the saved registry settings unchanged. Retag only after
            # every mirror image is present; container recreation uses --pull never.
            for service in SERVICES:
                self.command(["docker", "image", "tag", f"{MIRROR_IMAGE_PREFIX}-{service}:{version}", f"{IMAGE_PREFIX}-{service}:{version}"], timeout=30)
            return "南大源"
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            source = "南大源" if self.image_source == "nju" else "GHCR 和南大源均"
            raise ImagePullError(f"{source}拉取失败，未停止服务或切换版本") from exc

    def status(self, state: str, message: str, version: str = "", *, progress: int | None = None) -> None:
        atomic_write(self.directory / "status.json", json.dumps({"state": state, "message": message, "version": version, "progress": progress, "image_source": self.image_source}, ensure_ascii=False), 0o644)
        print(f"{state}: {message}" + (f" {progress}%" if progress is not None else ""), flush=True)

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
                self.image_source = payload.get("image_source", "ghcr")
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
                image_source = self.pull_images(version, environment)
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
                        self.status("success", f"更新完成（{image_source}），请刷新页面", version)
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
                    message = str(exc) if isinstance(exc, ImagePullError) else "更新失败，未切换版本，请查看宿主机更新服务日志"
                self.status("failed", message, version)
                print(f"Failure: {type(exc).__name__}: {exc}", file=sys.stderr)
                raise
            finally:
                request.unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["--sync-time"]:
        print(f"北京时间已校准：{sync_host_time()}（UTC+8，来源 {NTP_SERVER}）")
        sys.exit(0)
    config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    updater = Updater(config)
    if "--finalize" in sys.argv[2:]:
        try:
            state = json.loads((updater.directory / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        if state.get("state") == "running":
            (updater.directory / "request.json").unlink(missing_ok=True)
            updater.image_source = state.get("image_source", "ghcr")
            updater.status("failed", "更新服务异常中断，请查看宿主机日志", state.get("version", ""))
    else:
        updater.run()
