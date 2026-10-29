from __future__ import annotations

import json
import os
import re
from pathlib import Path

import httpx

RELEASES_URL = "https://api.github.com/repos/HITM1ss/QQmusicTSBot/releases/latest"
VERSION_PATTERN = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


def parse_version(value: str) -> tuple[int, int, int] | None:
    match = VERSION_PATTERN.fullmatch(value)
    return tuple(map(int, match.groups())) if match else None


def build_info() -> dict[str, str]:
    info: dict[str, str] = {}
    try:
        payload = json.loads(Path(__file__).with_name("build-info.json").read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            info = payload
    except (OSError, ValueError):
        pass
    return {
        "version": os.getenv("TSBOT_BUILD_VERSION") or info.get("version") or "development",
        "revision": os.getenv("TSBOT_BUILD_REVISION") or info.get("revision") or "",
    }


async def latest_release() -> dict[str, str]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get(RELEASES_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "QQmusicTSBot"})
        response.raise_for_status()
        release = response.json()
    tag = release.get("tag_name", "") if isinstance(release, dict) else ""
    if not isinstance(tag, str) or parse_version(tag) is None:
        raise ValueError("invalid release version")
    return {"version": tag, "url": f"https://github.com/HITM1ss/QQmusicTSBot/releases/tag/{tag}"}
