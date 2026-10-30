from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


def updater_directory() -> Path | None:
    value = os.getenv("TSBOT_UPDATER_DIR", "").strip()
    return Path(value) if value else None


def update_status() -> dict:
    directory = updater_directory()
    if not directory:
        return {"available": False, "state": "unavailable", "message": "宿主机更新服务尚未安装"}
    available = (directory / "installed").is_file()
    try:
        status = json.loads((directory / "status.json").read_text(encoding="utf-8"))
        if not isinstance(status, dict):
            raise ValueError("invalid status")
    except (OSError, ValueError):
        status = {"state": "idle" if available else "unavailable", "message": "更新服务已启用" if available else "宿主机更新服务尚未安装"}
    if not available:
        return {"available": False, "state": status.get("state", "unavailable") if status.get("state") in {"installing", "install_failed"} else "unavailable",
                "message": status.get("message", "") if status.get("state") in {"installing", "install_failed"} else "宿主机更新服务尚未安装"}
    if status.get("state") != "running":
        try:
            request = json.loads((directory / "request.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            pass
        else:
            status = {"state": "pending", "message": "更新请求已提交", "version": request["version"], "image_source": request.get("image_source", "ghcr")}
    return {"available": True, "state": status.get("state", "unknown"), "message": status.get("message", ""), "version": status.get("version", ""), "progress": status.get("progress"), "image_source": status.get("image_source", "ghcr")}


def submit_update(version: str, image_source: str = "ghcr") -> None:
    directory = updater_directory()
    if not directory or not (directory / "installed").is_file():
        raise RuntimeError("宿主机更新服务尚未安装")
    status = update_status()
    if status["state"] in {"pending", "running"}:
        raise FileExistsError("已有更新正在执行")
    fd, temp_path = tempfile.mkstemp(prefix=".request-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"version": version, "image_source": image_source}, handle)
        os.link(temp_path, directory / "request.json")
    finally:
        os.unlink(temp_path)
