from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import hmac
import math
import os
import random
import re
import time
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from .crypto import decrypt_text, encrypt_text
from .db import (
    create_db_and_tables,
    get_database_url,
    get_session,
    get_sqlite_db_path,
    new_session,
)
from .models import AdminCredential, HistoryItem, QueueItem, Secret
from .auth import (
    clear_session_cookie,
    create_session,
    get_admin_session,
    initialize_admin,
    invalidate_sessions,
    remove_initial_password_file,
    require_admin,
    require_csrf,
    set_session_cookie,
    verify_password,
    hash_password,
)
from .runtime_config import (
    DEFINITION_BY_KEY,
    get_value,
    initialize_runtime_settings,
    settings_payload,
    update_settings,
    voice_config_revision,
    write_voice_config,
)
from .managed_assets import ASSET_BY_KEY, asset_path, asset_payload, delete_asset, detect_image_type, save_asset, MAX_IMAGE_BYTES
from .qqmusic import QQMusicClient
from .serverquery import ServerQueryConfig, ServerQueryError, query_server_logs
from .voice_client import VoiceClient, VoiceStatus
from .config import settings
from .logger import logger
from .version import build_info, latest_release, parse_version
from .updates import submit_update, update_status

app = FastAPI(title="tsbot-backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["*"],
)

qqmusic = QQMusicClient()
voice = VoiceClient()

_QQMUSIC_QUEUE_META_PREFIX = "__qqmusic_quality__:"
_LOG_VIEW_MIN_LINES = 20
_LOG_VIEW_MAX_LINES = 500
_LOG_VIEW_MAX_BYTES = 256 * 1024
_LOG_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?<![A-Za-z0-9_])"
    r"([A-Za-z0-9_.-]*(?:authorization|cookie|token|password|secret|key)[A-Za-z0-9_.-]*|MUSIC_U|qm_keyst|uin)"
    r"(\s*[:=]\s*)([^\s,;，；]+)"
)
_INITIAL_PASSWORD_RE = re.compile(r"(初始密码\s*[：:]\s*)([^，,\s]+)")
_BEARER_TOKEN_RE = re.compile(r"(?i)(bearer\s+)([^\s,;，；]+)")

# Add OPTIONS handler for CORS preflight requests
@app.options("/{full_path:path}")
async def options_handler():
    return {"message": "OK"}


def _normalize_request_path(path: str) -> str:
    normalized = (path or "/").rstrip("/")
    return normalized or "/"


def _split_env_multiline(value: str | None) -> list[str]:
    raw = str(value or "").strip()
    if not raw:
        return []
    return [line.strip() for line in raw.replace("\\n", "\n").splitlines() if line.strip()]


def _get_request_api_token(request: Request) -> str:
    auth = (request.headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (request.headers.get("x-api-token") or "").strip()


def _path_requires_api_token(path: str) -> bool:
    if not settings.get_api_tokens():
        return False

    normalized = _normalize_request_path(path)
    return normalized == "/external" or normalized.startswith("/external/")


def _check_api_token(request: Request) -> str | None:
    tokens = settings.get_api_tokens()
    if not tokens:
        return None

    provided = _get_request_api_token(request)
    if not provided:
        return "missing api token"
    if any(hmac.compare_digest(provided, token) for token in tokens):
        return None
    return "invalid api token"


@app.middleware("http")
async def api_token_middleware(request: Request, call_next):
    if request.method == "OPTIONS" or not _path_requires_api_token(request.url.path):
        return await call_next(request)

    error = _check_api_token(request)
    if error is not None:
        return JSONResponse(
            status_code=401,
            content={"detail": error},
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)

_chat_task: asyncio.Task[None] | None = None
_current_queue_item_id: int | None = None
_pending_queue_item_id: int | None = None
_current_source_url: str = ""
_playback_lock = asyncio.Lock()
_play_request_generation: int = 0
_play_started_at: float | None = None
_paused_at: float | None = None
_paused_total_s: float = 0.0
_current_duration_ms: int = 0
_current_artist: str | None = None
_current_album: str | None = None
_current_artwork_url: str | None = None
_voice_unavailable_last_log: float = 0.0

_shuffle_enabled: bool = False
_repeat_mode: str = "none"  # "none", "all", "one"
_shuffle_queue: list[int] = []
_current_shuffle_index: int = -1

_recent_ts_chats: deque[dict] = deque(maxlen=100)
_TS_PLAYLIST_RESULTS_TTL_S = 300.0
_ts_playlist_results: dict[str, tuple[float, list[dict[str, str]]]] = {}

_main_loop: asyncio.AbstractEventLoop | None = None
_ts_desc_task: asyncio.Task[None] | None = None
_ts_desc_requested: bool = False
_ts_desc_last_sent_at: float = 0.0


class AddQueueRequest(BaseModel):
    track_id: str
    title: str
    artist: str = ""
    source_url: str


class AddQQMusicQueueRequest(BaseModel):
    song_mid: str
    title: str
    artist: str = ""
    album: str = ""
    play_now: bool = False
    quality: str = "320"
    album_mid: str = ""
    cover_url: str = ""
    duration_ms: int | None = None


class VolumeUpdateRequest(BaseModel):
    volume_percent: int


class AudioFxUpdateRequest(BaseModel):
    pan: float | None = None
    width: float | None = None
    swap_lr: bool | None = None
    bass_db: float | None = None
    reverb_mix: float | None = None


class TSClientDescriptionRequest(BaseModel):
    description: str


class ExternalPlayerActionRequest(BaseModel):
    action: str


class ExternalQueueRequest(BaseModel):
    source: str = "qqmusic"
    keywords: str = ""
    song_mid: str = ""
    title: str = ""
    artist: str = ""
    album: str = ""
    album_mid: str = ""
    duration_ms: int | None = None
    cover_url: str = ""
    quality: str = "320"
    play_now: bool = False


class AdminLoginRequest(BaseModel):
    username: str = "admin"
    password: str


class AdminPasswordChangeRequest(BaseModel):
    current_password: str
    new_password: str


class SettingsUpdateRequest(BaseModel):
    values: dict[str, object | None]
    apply: bool = True


_login_attempts: dict[str, tuple[int, float]] = {}


def _login_rate_limit_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _check_login_rate_limit(request: Request) -> None:
    key = _login_rate_limit_key(request)
    failures, blocked_until = _login_attempts.get(key, (0, 0.0))
    if blocked_until > time.monotonic():
        retry = max(1, int(blocked_until - time.monotonic()))
        raise HTTPException(status_code=429, detail=f"登录尝试过多，请 {retry} 秒后重试")
    if blocked_until:
        _login_attempts[key] = (failures, 0.0)


def _record_login_failure(request: Request) -> None:
    key = _login_rate_limit_key(request)
    failures, _ = _login_attempts.get(key, (0, 0.0))
    failures += 1
    delay = min(60, 2 ** max(0, failures - 3)) if failures >= 3 else 0
    _login_attempts[key] = (failures, time.monotonic() + delay if delay else 0.0)


def _remove_legacy_bilibili_queue_items(session: Session) -> int:
    """Discard queue entries that cannot be played after the source removal."""
    result = session.execute(
        delete(QueueItem).where(QueueItem.track_id.startswith("bilibili:"))
    )
    removed_count = int(result.rowcount or 0)
    if removed_count:
        session.commit()
        logger.info("removed %s queued entries from the retired music source", removed_count)
    return removed_count


@app.on_event("startup")
async def _startup() -> None:
    global _chat_task
    global _main_loop
    create_db_and_tables()

    bootstrap_session = new_session()
    try:
        initialize_admin(bootstrap_session)
        initialize_runtime_settings(bootstrap_session)
        _remove_legacy_bilibili_queue_items(bootstrap_session)
    finally:
        bootstrap_session.close()

    _main_loop = asyncio.get_running_loop()
    session = new_session()
    try:
        row = session.get(Secret, "voice_volume")
        if row and row.value:
            try:
                await voice.set_volume(int(row.value))
            except Exception:
                pass
    finally:
        session.close()

    _schedule_ts_description_update()

    if _chat_task is None or _chat_task.done():
        _chat_task = asyncio.create_task(_chat_command_worker())


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _chat_task
    if _chat_task is not None:
        _chat_task.cancel()
        _chat_task = None
    await voice.close()


async def _set_now_playing_queue_item(
    item_id: int | None,
    source_url: str = "",
    *,
    duration_ms: int | None = None,
    artist: str = "",
    album: str = "",
    artwork_url: str = "",
) -> None:
    global _current_queue_item_id, _current_source_url, _play_started_at, _paused_at, _paused_total_s, _current_duration_ms
    global _current_artist, _current_album, _current_artwork_url
    async with _playback_lock:
        _current_queue_item_id = item_id
        _current_source_url = (source_url or "").strip()

        if item_id is None:
            _play_started_at = None
            _paused_at = None
            _paused_total_s = 0.0
            _current_duration_ms = 0
            _current_artist = ""
            _current_album = ""
            _current_artwork_url = ""
        else:
            _play_started_at = time.monotonic()
            _paused_at = None
            _paused_total_s = 0.0
            _current_duration_ms = int(duration_ms or 0)
            _current_artist = (artist or "").strip()
            _current_album = (album or "").strip()
            _current_artwork_url = (artwork_url or "").strip()

    _schedule_ts_description_update()


async def _build_ts_description(*, queue_preview: int = 5) -> str:
    # Snapshot playback state under lock
    async with _playback_lock:
        cur_id = _current_queue_item_id
        paused = _play_started_at is not None and _paused_at is not None

    lines: list[str] = []
    title_lines = _split_env_multiline(settings.voice_description_title)
    intro_lines = _split_env_multiline(settings.voice_description_intro)
    if title_lines:
        lines.extend(title_lines)
        lines.append("")

    session = new_session()
    try:
        if cur_id:
            cur = session.get(QueueItem, int(cur_id))
            if cur:
                t = (cur.title or "").strip()
                a = (cur.artist or "").strip()
                state = "暂停" if paused else "正在播放"
                if a:
                    lines.append(f"{state}: {t} - {a}")
                else:
                    lines.append(f"{state}: {t}")
            else:
                lines.append("正在播放: (未知)")

            queue_items = _ordered_queue_items(session)
            current_index = next(
                (index for index, row in enumerate(queue_items) if int(row.id) == int(cur_id)),
                None,
            )
            rows = queue_items[current_index:] if current_index is not None else queue_items
            rows = rows[:int(queue_preview)]
        else:
            lines.append("状态: 空闲")
            rows = _ordered_queue_items(session)[:int(queue_preview)]

        if rows:
            lines.append("队列:")
            for i, r in enumerate(rows, 1):
                t = (r.title or "").strip()
                a = (r.artist or "").strip()
                if a:
                    lines.append(f"{i}. {t} - {a}")
                else:
                    lines.append(f"{i}. {t}")
        else:
            lines.append("队列: 空")
    finally:
        session.close()

    if intro_lines:
        lines.append("")
        lines.extend(intro_lines)

    desc = "\n".join(lines).strip()
    if len(desc) > 700:
        desc = desc[:700]
    return desc


def _schedule_ts_description_update() -> None:
    global _ts_desc_task, _ts_desc_requested

    _ts_desc_requested = True

    def _ensure_task() -> None:
        global _ts_desc_task
        if _ts_desc_task is None or _ts_desc_task.done():
            _ts_desc_task = asyncio.create_task(_ts_desc_worker())

    try:
        asyncio.get_running_loop()
        _ensure_task()
    except RuntimeError:
        # Called from a threadpool (sync FastAPI endpoints).
        if _main_loop is not None:
            _main_loop.call_soon_threadsafe(_ensure_task)


async def _ts_desc_worker() -> None:
    global _ts_desc_requested, _ts_desc_last_sent_at

    # Debounce bursts into one update.
    while _ts_desc_requested:
        _ts_desc_requested = False
        await asyncio.sleep(0.8)
        if _ts_desc_requested:
            continue

        # Rate limit: avoid spamming TS3 with clientupdate.
        now = time.time()
        if now - _ts_desc_last_sent_at < 3.0:
            await asyncio.sleep(3.0 - (now - _ts_desc_last_sent_at))

        try:
            desc = await _build_ts_description(queue_preview=5)
            await voice.set_client_description(desc)
            _ts_desc_last_sent_at = time.time()
        except Exception:
            pass


async def _take_now_playing_if_match(*, source_url: str) -> int | None:
    """If current playing source_url matches, clear it and return queue item id."""
    global _current_queue_item_id, _current_source_url, _play_started_at, _paused_at, _paused_total_s, _current_duration_ms
    global _current_artist, _current_album, _current_artwork_url
    src = (source_url or "").strip()
    async with _playback_lock:
        if not _current_queue_item_id:
            return None
        if not _current_source_url:
            return None
        if src != _current_source_url:
            return None
        item_id = _current_queue_item_id
        _current_queue_item_id = None
        _current_source_url = ""
        _play_started_at = None
        _paused_at = None
        _paused_total_s = 0.0
        _current_duration_ms = 0
        _current_artist = ""
        _current_album = ""
        _current_artwork_url = ""
        return item_id


async def _begin_play_request(item_id: int | None = None) -> int:
    global _play_request_generation, _pending_queue_item_id
    async with _playback_lock:
        _play_request_generation += 1
        _pending_queue_item_id = item_id
        return _play_request_generation


async def _invalidate_play_requests() -> int:
    global _play_request_generation, _pending_queue_item_id
    async with _playback_lock:
        _play_request_generation += 1
        _pending_queue_item_id = None
        return _play_request_generation


async def _is_play_request_current(request_generation: int) -> bool:
    async with _playback_lock:
        return request_generation == _play_request_generation


async def _clear_pending_queue_item_if_match(item_id: int | None) -> bool:
    global _pending_queue_item_id
    if item_id is None:
        return False
    async with _playback_lock:
        if _pending_queue_item_id != item_id:
            return False
        _pending_queue_item_id = None
        return True


async def _mark_playback_paused() -> None:
    global _paused_at
    async with _playback_lock:
        if _play_started_at is None:
            return
        if _paused_at is not None:
            return
        _paused_at = time.monotonic()

    _schedule_ts_description_update()


async def _mark_playback_resumed() -> None:
    global _paused_at, _paused_total_s
    async with _playback_lock:
        if _play_started_at is None:
            return
        if _paused_at is None:
            return
        _paused_total_s += max(0.0, time.monotonic() - _paused_at)
        _paused_at = None

    _schedule_ts_description_update()


async def _mark_playback_seeked(position_s: float) -> None:
    global _play_started_at, _paused_at, _paused_total_s
    target = max(0.0, float(position_s))
    now = time.monotonic()
    async with _playback_lock:
        if _current_queue_item_id is None or not _current_source_url:
            return
        _play_started_at = now - target
        _paused_total_s = 0.0
        if _paused_at is not None:
            _paused_at = now

    _schedule_ts_description_update()


def _resolve_playback_position_s(*, now_s: float, started_at: float, paused_at: float | None, paused_total_s: float) -> float:
    if paused_at is not None:
        pos = paused_at - started_at - paused_total_s
    else:
        pos = now_s - started_at - paused_total_s
    return max(0.0, pos)


async def _play_queue_item_internal(item_id: int, *, requested_by: str) -> bool:
    global _current_shuffle_index

    session = new_session()
    play_request_generation: int | None = None
    try:
        item = session.get(QueueItem, item_id)
        if not item:
            return False
        play_request_generation = await _begin_play_request(int(item.id))

        notice = ""
        duration_ms: int | None = item.duration
        artist = str(item.artist or "")
        album = str(item.album or "")
        artwork_url = str(item.cover_url or "")
        source_url = str(item.source_url or "")
        playback_source_url = source_url
        if item.track_id.startswith("qqmusic:"):
            # QQ Music URLs are short-lived. Refresh the URL whenever a queued
            # item starts instead of relying on the URL captured at enqueue time.
            song_mid = item.track_id.split(":", 1)[1].strip()
            if not song_mid:
                raise HTTPException(status_code=400, detail="qqmusic song_mid is empty")

            cookie = _get_admin_qqmusic_cookie(session)
            qqmusic.set_cookie(cookie)
            quality = _extract_qqmusic_queue_quality(source_url)
            playback_source_url = await qqmusic.get_music_url_simple(song_mid, quality)
            if not playback_source_url:
                raise HTTPException(
                    status_code=404,
                    detail="无法获取 QQ 音乐播放链接，可能需要 VIP 会员或该歌曲不可用",
                )

            if not await _is_play_request_current(play_request_generation):
                return True

            item.source_url = _encode_qqmusic_queue_source(quality, playback_source_url)
            session.add(item)
            session.commit()
        else:
            item.source_url = playback_source_url

        if not await _is_play_request_current(play_request_generation):
            return True

        if _shuffle_enabled and item_id in _shuffle_queue:
            _current_shuffle_index = _shuffle_queue.index(item_id)

        await _set_now_playing_queue_item(
            int(item.id),
            playback_source_url,
            duration_ms=duration_ms,
            artist=artist,
            album=album,
            artwork_url=artwork_url,
        )

        if not await _is_play_request_current(play_request_generation):
            await _take_now_playing_if_match(source_url=playback_source_url)
            return True

        await voice.play(source_url=playback_source_url, title=item.title, requested_by=requested_by, notice=notice)

        if not await _is_play_request_current(play_request_generation):
            return True

        hist = HistoryItem(
            track_id=item.track_id,
            title=item.title,
            artist=item.artist,
            album=item.album,
            duration=item.duration,
            cover_url=item.cover_url,
            source_url=playback_source_url,
            requested_by=requested_by,
        )
        session.add(hist)
        session.commit()
        return True
    finally:
        if play_request_generation is not None:
            await _clear_pending_queue_item_if_match(item_id)
        session.close()


async def _delete_queue_item(item_id: int) -> None:
    global _shuffle_queue, _current_shuffle_index
    
    session = new_session()
    try:
        row = session.get(QueueItem, item_id)
        if row is not None:
            session.delete(row)
            session.commit()
            
            # Update shuffle queue if item was in it
            if _shuffle_enabled and item_id in _shuffle_queue:
                removed_index = _shuffle_queue.index(item_id)
                _shuffle_queue.remove(item_id)
                
                # Adjust current shuffle index if necessary
                if removed_index <= _current_shuffle_index:
                    # The next item shifts into the removed item's slot. A
                    # current index of -1 makes auto-play select that slot.
                    _current_shuffle_index = max(-1, _current_shuffle_index - 1)
    finally:
        session.close()

    _schedule_ts_description_update()


async def _clear_queue_internal(session: Session) -> dict:
    global _shuffle_queue, _current_shuffle_index

    removed_count = int(session.execute(select(func.count(QueueItem.id))).scalar() or 0)
    session.execute(delete(QueueItem))
    session.commit()

    _shuffle_queue = []
    _current_shuffle_index = -1
    await _invalidate_play_requests()
    await _set_now_playing_queue_item(None)

    playback_stopped = False
    try:
        await voice.stop()
        playback_stopped = True
    except Exception:
        pass

    _schedule_ts_description_update()
    return {"ok": True, "removed_count": removed_count, "playback_stopped": playback_stopped}


# Alias for backward compatibility
_remove_queue_item_internal = _delete_queue_item


_AUTO_SKIP_PLAYBACK_STATUS_CODES = frozenset({402, 403, 404, 410, 422})
_QUEUE_CONFIGURATION_ERROR_DETAILS = frozenset({
    "admin qqmusic cookie not set",
    "failed to decrypt admin qqmusic cookie",
})
_MALFORMED_QUEUE_ITEM_MARKERS = (
    "qqmusic song_mid is empty",
)


def _should_auto_skip_unplayable_queue_item(exc: HTTPException) -> bool:
    """Return whether an error means this queue item can never be played.

    Authentication, rate-limit, and upstream service failures are intentionally
    retained: deleting every queued song while an account or music service is
    temporarily unavailable would be more harmful than pausing playback.
    """
    detail = str(exc.detail or "").strip()
    normalized_detail = detail.lower()

    if detail in _QUEUE_CONFIGURATION_ERROR_DETAILS:
        return False
    if exc.status_code in _AUTO_SKIP_PLAYBACK_STATUS_CODES:
        return True
    return exc.status_code == 400 and any(marker in normalized_detail for marker in _MALFORMED_QUEUE_ITEM_MARKERS)


def _compact_playback_failure_reason(reason: str, *, limit: int = 160) -> str:
    compact = " ".join(str(reason or "").split())
    if not compact:
        return "音源不可用"
    if len(compact) <= limit:
        return compact
    return f"{compact[:limit - 1]}…"


async def _skip_unplayable_queue_item(
    item_id: int,
    *,
    reason: str,
    start_after_id: int | None = None,
) -> None:
    """Discard a permanently unplayable queue item and continue playback."""
    message = _compact_playback_failure_reason(reason)
    logger.warning("skip unplayable queue item id=%s: %s", item_id, message)
    await _delete_queue_item(item_id)
    try:
        await voice.send_notice(
            f"无法播放，已跳过: #{item_id}\n{message}\n将播放下一首",
            target_mode=2,
        )
    except Exception:
        pass
    await _auto_play_next_from_queue(
        start_after_id=item_id if start_after_id is None else start_after_id,
    )


async def _auto_play_next_from_queue(*, start_after_id: int | None = None) -> None:
    global _current_shuffle_index, _shuffle_queue
    
    session = new_session()
    try:
        if _shuffle_enabled:
            # Queue items can be added after shuffle mode was enabled. Keep the
            # shuffled order in sync while preserving the existing random order.
            queued_ids = [
                int(row.id)
                for row in _ordered_queue_items(session)
            ]
            queued_id_set = set(queued_ids)
            current_shuffle_id = (
                _shuffle_queue[_current_shuffle_index]
                if 0 <= _current_shuffle_index < len(_shuffle_queue)
                else None
            )
            _shuffle_queue[:] = [item_id for item_id in _shuffle_queue if item_id in queued_id_set]
            if current_shuffle_id is not None and current_shuffle_id in _shuffle_queue:
                _current_shuffle_index = _shuffle_queue.index(current_shuffle_id)
            elif current_shuffle_id is not None:
                _current_shuffle_index = -1
            missing_ids = [item_id for item_id in queued_ids if item_id not in _shuffle_queue]
            if missing_ids:
                random.shuffle(missing_ids)
                _shuffle_queue.extend(missing_ids)
            if not _shuffle_queue:
                return

            # Play next shuffled track
            next_index = _current_shuffle_index + 1
            
            if next_index >= len(_shuffle_queue):
                if _repeat_mode == "all":
                    next_index = 0
                else:
                    return  # End of shuffled queue
            
            item_id = _shuffle_queue[next_index]
            _current_shuffle_index = next_index
        else:
            # Regular queue order
            cursor_id = start_after_id if start_after_id is not None else _current_queue_item_id
            if start_after_id is None and _current_queue_item_id and _repeat_mode == "one":
                # Repeat current track
                item_id = _current_queue_item_id
            else:
                # Get next track in regular order
                if cursor_id:
                    queue_items = _ordered_queue_items(session)
                    cursor_index = next(
                        (index for index, row in enumerate(queue_items) if int(row.id) == int(cursor_id)),
                        None,
                    )
                    nxt = (
                        queue_items[cursor_index + 1]
                        if cursor_index is not None and cursor_index + 1 < len(queue_items)
                        else (queue_items[0] if cursor_index is None and queue_items else None)
                    )
                else:
                    nxt = session.execute(
                        select(QueueItem)
                        .order_by(*_queue_order_by())
                        .limit(1)
                    ).scalars().first()
                
                if not nxt:
                    if _repeat_mode == "all":
                        # Loop back to beginning
                        nxt = session.execute(
                            select(QueueItem)
                            .order_by(*_queue_order_by())
                            .limit(1)
                        ).scalars().first()
                        if not nxt:
                            return
                    else:
                        return  # End of queue
                
                item_id = int(nxt.id)
    finally:
        session.close()

    try:
        played = await _play_queue_item_internal(item_id, requested_by="auto")
    except HTTPException as exc:
        if _should_auto_skip_unplayable_queue_item(exc):
            await _skip_unplayable_queue_item(item_id, reason=str(exc.detail or ""))
            return
        raise
    if not played:
        # A row may disappear between selection and playback. Do not let the
        # A stale queue position must not block later playable items.
        await _skip_unplayable_queue_item(item_id, reason="队列歌曲已不存在或不可播放")


def _serialize_queue_item(row: QueueItem) -> dict:
    if row.track_id.startswith("qqmusic:"):
        source_url = _strip_qqmusic_queue_meta(row.source_url)
    else:
        source_url = row.source_url
    track_ref = _build_track_reference(str(row.track_id or ""))
    return {
        "id": row.id,
        "track_id": row.track_id,
        **track_ref,
        "title": row.title,
        "artist": row.artist,
        "album": row.album,
        "duration": row.duration / 1000.0 if row.duration else None,
        "artwork": row.cover_url,
        "source_url": source_url,
    }


def _build_track_reference(track_id: str) -> dict[str, object]:
    raw = str(track_id or "").strip()
    if not raw:
        return {"source": "unknown"}

    source, _, suffix = raw.partition(":")
    source = source.strip().lower() or "unknown"
    suffix = suffix.strip()

    payload: dict[str, object] = {"source": source}
    if source == "qqmusic" and suffix:
        payload["song_mid"] = suffix
    return payload


def _serialize_history_item(row: HistoryItem) -> dict:
    return {
        "id": row.id,
        "played_at": row.played_at.isoformat(),
        "track_id": row.track_id,
        **_build_track_reference(str(row.track_id or "")),
        "title": row.title,
        "artist": row.artist,
        "album": row.album,
        "duration": row.duration / 1000.0 if row.duration else None,
        "artwork": row.cover_url,
        "source_url": row.source_url,
        "requested_by": row.requested_by,
    }


def _coerce_positive_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _coerce_non_negative_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _normalize_qqmusic_quality(value: object, *, default: str = "320") -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "m4a": "m4a",
        "128": "128",
        "128k": "128",
        "320": "320",
        "320k": "320",
    }
    return aliases.get(raw, default)


def _encode_qqmusic_queue_source(quality: object, source_url: str = "") -> str:
    normalized = _normalize_qqmusic_quality(quality)
    resolved_source_url = str(source_url or "").strip()
    if resolved_source_url:
        return f"{_QQMUSIC_QUEUE_META_PREFIX}{normalized}|{resolved_source_url}"
    return f"{_QQMUSIC_QUEUE_META_PREFIX}{normalized}"


def _extract_qqmusic_queue_quality(source_url: object) -> str:
    raw = str(source_url or "").strip()
    if raw.startswith(_QQMUSIC_QUEUE_META_PREFIX):
        quality_raw, _, _rest = raw[len(_QQMUSIC_QUEUE_META_PREFIX) :].partition("|")
        return _normalize_qqmusic_quality(quality_raw)
    return "320"


def _strip_qqmusic_queue_meta(source_url: object) -> str:
    raw = str(source_url or "").strip()
    if not raw.startswith(_QQMUSIC_QUEUE_META_PREFIX):
        return raw
    _quality, sep, rest = raw[len(_QQMUSIC_QUEUE_META_PREFIX) :].partition("|")
    return rest.strip() if sep else ""


def _extract_qqmusic_artist_names(song: dict) -> str:
    artists = song.get("singer") or song.get("artists") or song.get("artist") or []
    if isinstance(artists, str):
        return artists.strip()
    if not isinstance(artists, list):
        return ""
    names = [
        str(artist.get("name") if isinstance(artist, dict) else artist or "").strip()
        for artist in artists
    ]
    return ", ".join([name for name in names if name])


def _normalize_qqmusic_song(song: dict) -> dict | None:
    song_mid = str(song.get("mid") or song.get("songmid") or "").strip()
    if not song_mid:
        return None

    album = song.get("album") if isinstance(song.get("album"), dict) else {}
    album_mid = str((album or {}).get("mid") or song.get("albummid") or "").strip()
    album_name = str((album or {}).get("name") or song.get("albumname") or "").strip()
    interval = _coerce_positive_int(song.get("interval"))
    duration_ms = interval * 1000 if interval is not None else None
    artwork_url = qqmusic.get_song_cover_image(album_mid) if album_mid else ""
    title = str(
        song.get("name")
        or song.get("songname")
        or song.get("title")
        or song.get("songorig")
        or song_mid
    ).strip()

    return {
        "source": "qqmusic",
        "track_id": f"qqmusic:{song_mid}",
        "song_mid": song_mid,
        "title": title,
        "artist": _extract_qqmusic_artist_names(song),
        "album": album_name,
        "album_mid": album_mid,
        "duration_ms": duration_ms,
        "artwork_url": artwork_url,
    }


def _normalize_qqmusic_search_items(songs: list[dict]) -> list[dict]:
    items: list[dict] = []
    for song in songs:
        if not isinstance(song, dict):
            continue
        normalized = _normalize_qqmusic_song(song)
        if normalized is not None:
            items.append(normalized)
    return items


def _extract_qqmusic_playlist_search_items(raw: dict) -> list[dict[str, str]]:
    """Normalize QQ Music playlist-search results across response variants."""
    body = (((raw or {}).get("req") or {}).get("data") or {}).get("body") or {}
    if not isinstance(body, dict):
        return []

    candidates: list[dict] = []
    for key in ("songlist", "playlist", "diss", "disslist", "song_list"):
        value = body.get(key)
        if isinstance(value, dict):
            value = value.get("list") or value.get("items") or value.get("data") or []
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))

    items: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    for playlist in candidates:
        playlist_id = str(
            playlist.get("dissid")
            or playlist.get("disstid")
            or playlist.get("tid")
            or playlist.get("id")
            or ""
        ).strip()
        if not playlist_id:
            continue
        if playlist_id in seen_ids:
            continue
        seen_ids.add(playlist_id)

        creator = playlist.get("creator") or playlist.get("owner") or ""
        if isinstance(creator, dict):
            creator = creator.get("nick") or creator.get("nickname") or creator.get("name") or ""
        track_count = (
            playlist.get("song_count")
            or playlist.get("songCount")
            or playlist.get("songnum")
            or playlist.get("song_num")
            or ""
        )
        cover_url = str(
            playlist.get("logo")
            or playlist.get("imgurl")
            or playlist.get("picurl")
            or playlist.get("disscover")
            or playlist.get("diss_cover")
            or ""
        ).strip()
        if cover_url.startswith("//"):
            cover_url = f"https:{cover_url}"
        play_count = (
            playlist.get("listennum")
            or playlist.get("play_count")
            or playlist.get("playCount")
            or ""
        )
        items.append(
            {
                "id": playlist_id,
                "name": str(
                    playlist.get("dissname")
                    or playlist.get("name")
                    or playlist.get("title")
                    or playlist_id
                ).strip(),
                "creator": str(creator).strip(),
                "track_count": str(track_count).strip(),
                "cover_url": cover_url,
                "play_count": str(play_count).strip(),
            }
        )
    return items


def _extract_qqmusic_playlist_tracks(raw: dict) -> tuple[str, list[dict]]:
    """Extract a QQ Music playlist name and raw song list."""
    cdlist = (raw or {}).get("cdlist") or []
    if isinstance(cdlist, list) and cdlist and isinstance(cdlist[0], dict):
        playlist = cdlist[0]
        tracks = playlist.get("songlist") or []
        return (
            str(playlist.get("dissname") or playlist.get("name") or "").strip(),
            [track for track in tracks if isinstance(track, dict)] if isinstance(tracks, list) else [],
        )

    tracks = (raw or {}).get("songlist") or []
    return (
        str((raw or {}).get("dissname") or (raw or {}).get("name") or "").strip(),
        [track for track in tracks if isinstance(track, dict)] if isinstance(tracks, list) else [],
    )


async def _load_qqmusic_playlist_tracks(playlist_id: str) -> tuple[str, list[dict]]:
    session = new_session()
    try:
        qq_cookie = _get_admin_qqmusic_cookie(session)
    finally:
        session.close()
    qqmusic.set_cookie(qq_cookie)
    return _extract_qqmusic_playlist_tracks(await qqmusic.get_song_list(playlist_id))


@app.get("/voice/status")
async def voice_status() -> dict:
    global _voice_unavailable_last_log
    voice_connected = True
    try:
        st = await voice.get_status()
    except Exception as exc:
        voice_connected = False
        now = time.monotonic()
        if now - _voice_unavailable_last_log >= 30:
            logger.warning("voice-service 暂时不可用（%s），状态接口将返回离线状态", type(exc).__name__)
            _voice_unavailable_last_log = now
        st = VoiceStatus(
            state="STATE_ERROR",
            now_playing_title="",
            now_playing_source_url="",
            volume_percent=100,
            config_revision="",
        )

    state_map = {
        "STATE_IDLE": "idle",
        "STATE_PLAYING": "playing",
        "STATE_PAUSED": "paused",
        "STATE_BUFFERING": "buffering",
        "STATE_ERROR": "error",
        "STATE_UNSPECIFIED": "idle",
    }
    state = state_map.get(str(st.state or "").strip().upper(), "idle")

    async with _playback_lock:
        qid = _current_queue_item_id
        started_at = _play_started_at
        paused_at = _paused_at
        paused_total_s = _paused_total_s
        duration_ms = _current_duration_ms
        cached_artist = _current_artist
        cached_album = _current_album
        cached_artwork_url = _current_artwork_url

    # If backend has no notion of current track, treat as idle.
    if qid is None:
        state = "idle"

    current_time_s = 0.0
    if started_at is not None and qid is not None:
        current_time_s = _resolve_playback_position_s(
            now_s=time.monotonic(),
            started_at=started_at,
            paused_at=paused_at,
            paused_total_s=paused_total_s,
        )
        if paused_at is not None:
            state = "paused"

    if duration_ms > 0:
        current_time_s = min(current_time_s, duration_ms / 1000.0)

    now_playing_artist = (cached_artist or "").strip()
    now_playing_album = (cached_album or "").strip()
    artwork_url = (cached_artwork_url or "").strip()
    if qid is not None and not now_playing_artist:
        session = new_session()
        try:
            row = session.get(QueueItem, int(qid))
            if row is not None:
                now_playing_artist = str(row.artist or "")
        finally:
            session.close()

    return {
        "state": state,
        "now_playing_title": st.now_playing_title,
        "now_playing_source_url": st.now_playing_source_url,
        "now_playing_artist": now_playing_artist,
        "now_playing_album": now_playing_album,
        "artwork_url": artwork_url,
        "track_id": qid,
        "current_time": current_time_s,
        "duration": (duration_ms / 1000.0) if duration_ms > 0 else 0.0,
        "volume_percent": st.volume_percent,
        "is_shuffled": _shuffle_enabled,
        "repeat_mode": _repeat_mode,
        "voice_connected": voice_connected,
        "voice_config_revision": st.config_revision,
    }


@app.put("/voice/volume")
async def set_voice_volume(
    req: VolumeUpdateRequest,
    session: Session = Depends(get_session),
) -> dict:
    v = int(req.volume_percent)
    if v < 0:
        v = 0
    if v > 200:
        v = 200

    await voice.set_volume(v)

    row = session.get(Secret, "voice_volume")
    if not row:
        row = Secret(key="voice_volume", value=str(v))
        session.add(row)
    else:
        row.value = str(v)
    session.commit()
    return {"ok": True, "volume_percent": v}


@app.get("/voice/fx")
async def get_voice_fx() -> dict:
    fx = await voice.get_audio_fx()
    return {
        "pan": fx.pan,
        "width": fx.width,
        "swap_lr": fx.swap_lr,
        "bass_db": fx.bass_db,
        "reverb_mix": fx.reverb_mix,
    }


@app.put("/voice/fx")
async def set_voice_fx(req: AudioFxUpdateRequest) -> dict:
    await voice.set_audio_fx(
        pan=req.pan,
        width=req.width,
        swap_lr=req.swap_lr,
        bass_db=req.bass_db,
        reverb_mix=req.reverb_mix,
    )
    fx = await voice.get_audio_fx()
    return {
        "ok": True,
        "pan": fx.pan,
        "width": fx.width,
        "swap_lr": fx.swap_lr,
        "bass_db": fx.bass_db,
        "reverb_mix": fx.reverb_mix,
    }


@app.post("/voice/play")
async def voice_play() -> dict:
    st = await voice.get_status()
    cur = str(st.state or "").strip().upper()
    if cur == "STATE_IDLE":
        async with _playback_lock:
            pending_item_id = _pending_queue_item_id
        if pending_item_id is not None:
            return {"ok": True, "action": "pending"}
        await _auto_play_next_from_queue()
        return {"ok": True, "action": "play_next"}
    if cur == "STATE_PAUSED":
        await _mark_playback_resumed()
        await voice.resume()
        return {"ok": True, "action": "resume"}
    return {"ok": True, "action": "noop"}


@app.post("/voice/pause")
async def voice_pause() -> dict:
    await _mark_playback_paused()
    await voice.pause()
    return {"ok": True}


@app.post("/voice/next")
async def voice_next() -> dict:
    global _current_shuffle_index, _shuffle_queue
    current_item_id = None
    pending_item_id = None
    async with _playback_lock:
        current_item_id = _current_queue_item_id
        pending_item_id = _pending_queue_item_id

    active_item_id = current_item_id or pending_item_id

    if active_item_id:
        await _remove_queue_item_internal(active_item_id)
    await _invalidate_play_requests()

    if _shuffle_enabled and _shuffle_queue:
        # Handle shuffled next
        next_index = _current_shuffle_index + 1
        
        if next_index >= len(_shuffle_queue):
            if _repeat_mode == "all":
                next_index = 0
            else:
                await _set_now_playing_queue_item(None)
                await voice.skip()
                return {"ok": True, "action": "end_of_queue"}
        
        item_id = _shuffle_queue[next_index]
        _current_shuffle_index = next_index
        
        try:
            played = await _play_queue_item_internal(item_id, requested_by="next")
        except HTTPException as exc:
            detail = str(exc.detail or "").strip()
            if _should_auto_skip_unplayable_queue_item(exc):
                await _skip_unplayable_queue_item(item_id, reason=detail)
                return {"ok": True, "action": "skipped_unplayable"}
            raise
        if not played:
            await _skip_unplayable_queue_item(item_id, reason="队列歌曲已不存在或不可播放")
            return {"ok": True, "action": "skipped_unplayable"}
        return {"ok": True, "action": "play_shuffled_next"}
    else:
        # Regular next behavior - just play next without removing current
        start_after_id = active_item_id
        await _set_now_playing_queue_item(None)
        await voice.skip()
        await _auto_play_next_from_queue(start_after_id=start_after_id)
        return {"ok": True, "action": "next"}


@app.post("/voice/skip")
async def voice_skip() -> dict:
    """Skip current song: remove from queue and play next"""
    global _current_shuffle_index, _shuffle_queue
    
    # Get current playing item to remove it
    current_item_id = None
    pending_item_id = None
    async with _playback_lock:
        current_item_id = _current_queue_item_id
        pending_item_id = _pending_queue_item_id

    active_item_id = current_item_id or pending_item_id
    
    if active_item_id:
        # Remove current song from queue
        await _remove_queue_item_internal(active_item_id)
        await _invalidate_play_requests()
        
        # Stop current playback
        await _set_now_playing_queue_item(None)
        await voice.skip()
        
        # Auto play next song
        await _auto_play_next_from_queue(start_after_id=active_item_id)
        return {"ok": True, "action": "skipped_and_next", "removed_track_id": active_item_id}
    else:
        await _invalidate_play_requests()
        return {"ok": True, "action": "no_current_track", "message": "当前没有正在播放的歌曲"}


@app.post("/voice/previous")
async def voice_previous() -> dict:
    global _current_shuffle_index, _shuffle_queue
    
    if _shuffle_enabled and _shuffle_queue:
        # Handle shuffled previous
        prev_index = _current_shuffle_index - 1
        
        if prev_index < 0:
            if _repeat_mode == "all":
                prev_index = len(_shuffle_queue) - 1
            else:
                return {"ok": True, "message": "Beginning of shuffled queue"}
        
        item_id = _shuffle_queue[prev_index]
        _current_shuffle_index = prev_index
        
        await _play_queue_item_internal(item_id, requested_by="previous")
        return {"ok": True, "action": "play_shuffled_previous"}
    else:
        # Handle regular previous
        session = new_session()
        try:
            async with _playback_lock:
                cursor_item_id = _current_queue_item_id or _pending_queue_item_id

            if cursor_item_id:
                queue_items = _ordered_queue_items(session)
                current_index = next(
                    (index for index, row in enumerate(queue_items) if int(row.id) == int(cursor_item_id)),
                    None,
                )
                prev = queue_items[current_index - 1] if current_index and current_index > 0 else None
                
                if prev:
                    await _play_queue_item_internal(int(prev.id), requested_by="previous")
                    return {"ok": True, "action": "play_previous"}
                elif _repeat_mode == "all":
                    # Go to last track
                    last = session.execute(
                        select(QueueItem)
                        .order_by(QueueItem.queue_position.desc(), QueueItem.id.desc())
                        .limit(1)
                    ).scalars().first()
                    
                    if last:
                        await _play_queue_item_internal(int(last.id), requested_by="previous")
                        return {"ok": True, "action": "play_last"}
        finally:
            session.close()
        
        return {"ok": True, "message": "No previous track available"}


class SeekRequest(BaseModel):
    time: float


class LyricLine(BaseModel):
    time: float
    text: str


class LyricsResponse(BaseModel):
    lyrics: list[LyricLine]


@app.post("/voice/seek")
async def voice_seek(req: SeekRequest) -> dict:
    if not math.isfinite(req.time):
        raise HTTPException(status_code=400, detail="invalid seek time")

    async with _playback_lock:
        has_track = _current_queue_item_id is not None and bool(_current_source_url)
        duration_ms = int(_current_duration_ms or 0)

    if not has_track:
        raise HTTPException(status_code=400, detail="当前没有正在播放的歌曲")

    target_time_s = max(0.0, float(req.time))
    if duration_ms > 0:
        target_time_s = min(target_time_s, duration_ms / 1000.0)

    try:
        await voice.seek(target_time_s)
    except RuntimeError as e:
        detail = str(e) or "seek failed"
        if "no active playback" in detail.lower():
            raise HTTPException(status_code=409, detail=detail)
        raise HTTPException(status_code=500, detail=detail)

    await _mark_playback_seeked(target_time_s)
    return {"ok": True, "time": target_time_s}


class ShuffleRequest(BaseModel):
    enabled: bool


async def _set_shuffle_enabled(enabled: bool) -> dict:
    global _shuffle_enabled, _shuffle_queue, _current_shuffle_index

    _shuffle_enabled = bool(enabled)

    if _shuffle_enabled:
        session = new_session()
        try:
            queue_items = _ordered_queue_items(session)
            _shuffle_queue = [int(item.id) for item in queue_items]
            random.shuffle(_shuffle_queue)

            if _current_queue_item_id:
                try:
                    _current_shuffle_index = _shuffle_queue.index(_current_queue_item_id)
                except ValueError:
                    _current_shuffle_index = -1
            else:
                _current_shuffle_index = -1
        finally:
            session.close()
    else:
        _shuffle_queue = []
        _current_shuffle_index = -1

    _schedule_ts_description_update()
    return {"ok": True, "enabled": _shuffle_enabled}


@app.post("/voice/shuffle")
async def voice_shuffle(req: ShuffleRequest) -> dict:
    return await _set_shuffle_enabled(req.enabled)


class RepeatRequest(BaseModel):
    mode: str  # "none", "all", "one"


@app.post("/voice/repeat")
async def voice_repeat(req: RepeatRequest) -> dict:
    global _repeat_mode
    
    if req.mode in ["none", "all", "one"]:
        _repeat_mode = req.mode
    else:
        _repeat_mode = "none"
    
    _schedule_ts_description_update()
    return {"ok": True, "mode": _repeat_mode}


def _parse_lrc_to_lines(lrc: str) -> list[LyricLine]:
    lines: list[LyricLine] = []
    for raw in (lrc or "").splitlines():
        s = raw.strip()
        if not s:
            continue
        if not s.startswith("["):
            continue
        parts = s.split("]")
        if len(parts) < 2:
            continue
        text = "]".join(parts[1:]).strip()
        for tag in parts[:-1]:
            t = tag.lstrip("[").strip()
            if not t:
                continue
            if ":" not in t:
                continue
            mm, rest = t.split(":", 1)
            try:
                minutes = int(mm)
            except ValueError:
                continue
            try:
                seconds = float(rest)
            except ValueError:
                continue
            ts = minutes * 60.0 + seconds
            lines.append(LyricLine(time=ts, text=text))
    lines.sort(key=lambda x: x.time)
    return lines


@app.get("/lyrics/{queue_item_id}", response_model=LyricsResponse)
async def lyrics(queue_item_id: int) -> LyricsResponse:
    session = new_session()
    try:
        item = session.get(QueueItem, queue_item_id)
        if not item:
            raise HTTPException(status_code=404, detail="not found")
        track_id = str(item.track_id or "")
        title = str(item.title or "")
        artist = str(item.artist or "")
    finally:
        session.close()

    if track_id.startswith("qqmusic:"):
        # QQ 音乐歌词
        song_mid = track_id.split(":", 1)[1]
        try:
            # 设置 QQ 音乐 admin cookie
            session2 = new_session()
            try:
                cookie = _get_admin_qqmusic_cookie(session2)
                qqmusic.set_cookie(cookie)
            finally:
                session2.close()
            
            # 获取 QQ 音乐歌词
            data = await qqmusic.get_song_lyric(song_mid)
            lrc = data.get("lyric", "") if data else ""
            return LyricsResponse(lyrics=_parse_lrc_to_lines(str(lrc)))
        except Exception:
            return LyricsResponse(lyrics=[])

    else:
        return LyricsResponse(lyrics=[])


def _get_admin_qqmusic_cookie(session: Session) -> str:
    row = session.get(Secret, "qqmusic_cookie")
    if not row:
        raise HTTPException(status_code=400, detail="admin qqmusic cookie not set")
    try:
        cookie = decrypt_text(row.value).strip()
    except Exception:
        raise HTTPException(status_code=500, detail="failed to decrypt admin qqmusic cookie")
    if not cookie:
        raise HTTPException(status_code=400, detail="admin qqmusic cookie not set")
    return cookie


def _require_admin_token(request: Request) -> None:
    session = new_session()
    try:
        _, admin_session = require_admin(request, session)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            require_csrf(request, admin_session)
    finally:
        session.close()


def _format_help() -> str:
    lines = [
        "Commands (no prefix):",
        "帮助|help - show this help",
        "状态|now - show now playing",
    ]
    lines.extend([
        "搜索|search <keywords> - search QQ Music songs",
        "增加|add <song_mid|keywords> - add a QQ Music song to the queue",
        "点歌 <song_mid|keywords> - add a QQ Music song to play next",
        "播放|play [song_mid|keywords] - play a QQ Music song; no argument plays the first queue item",
        "歌单|playlist <keywords> - search QQ Music playlists",
        "选择|select <number> - add a playlist from the last QQ Music playlist search",
    ])
    lines.extend([
        "队列|queue - show queue",
        "清空|clear - clear the current queue",
        "顺序播放|order / 随机播放|random - switch queue playback mode",
        "暂停|pause / 恢复|resume / 停止|stop / 跳过|skip",
        "音量|vol <0-200> - set volume",
        "音效|fx - show audio fx",
        "fx pan <-1..1> / fx width <0..3> / fx swap <on|off> / fx bass <0..18> / fx reverb <0..1> / fx reset",
    ])
    return "\n".join(lines)


def _try_parse_qqmusic_song_mid(s: str) -> str | None:
    token = (s or "").strip()
    explicit_mid = False
    for prefix in ("mid:", "songmid:"):
        if token.lower().startswith(prefix):
            token = token[len(prefix) :].strip()
            explicit_mid = True
            break
    if explicit_mid and re.fullmatch(r"[A-Za-z0-9]{6,64}", token):
        return token
    if re.fullmatch(r"(?=.*[A-Za-z])(?=.*\d)[A-Za-z0-9]{6,64}", token):
        return token
    return None


def _ts_playlist_result_key(*, invoker_unique_id: str, invoker_name: str) -> str:
    unique_id = (invoker_unique_id or "").strip()
    if unique_id:
        return f"uid:{unique_id}"
    name = (invoker_name or "").strip()
    return f"name:{name or '__anonymous__'}"


def _remember_ts_playlist_results(key: str, playlists: list[dict[str, str]]) -> None:
    now = time.monotonic()
    expired_keys = [
        stored_key
        for stored_key, (expires_at, _items) in _ts_playlist_results.items()
        if expires_at <= now
    ]
    for expired_key in expired_keys:
        _ts_playlist_results.pop(expired_key, None)
    _ts_playlist_results[key] = (now + _TS_PLAYLIST_RESULTS_TTL_S, playlists)


def _get_ts_playlist_results(key: str) -> list[dict[str, str]]:
    stored = _ts_playlist_results.get(key)
    if stored is None:
        return []
    expires_at, playlists = stored
    if expires_at <= time.monotonic():
        _ts_playlist_results.pop(key, None)
        return []
    return playlists


def _queue_order_by() -> tuple[object, object]:
    return QueueItem.queue_position.asc(), QueueItem.id.asc()


def _ordered_queue_items(session: Session) -> list[QueueItem]:
    return session.execute(select(QueueItem).order_by(*_queue_order_by())).scalars().all()


def _queue_position_for_new_item(
    session: Session,
    *,
    prioritize: bool,
    active_item_id: int | None = None,
) -> int:
    """Return a position for a new queue item and make room when needed."""
    if not prioritize:
        max_position = session.execute(select(func.max(QueueItem.queue_position))).scalar()
        try:
            return max(0, int(max_position or 0)) + 1
        except (TypeError, ValueError):
            return 1

    rows = _ordered_queue_items(session)
    active_item = next(
        (row for row in rows if int(row.id) == int(active_item_id)),
        None,
    ) if active_item_id is not None else None

    if active_item is not None:
        # Keep the song already playing first. The new request then becomes the
        # first waiting song and will play next without interrupting playback.
        active_item.queue_position = 0
        rows = [row for row in rows if int(row.id) != int(active_item.id)]

    for position, row in enumerate(rows, start=2):
        row.queue_position = position
    return 1


def _prioritize_existing_queue_item(
    session: Session,
    item: QueueItem,
    *,
    active_item_id: int | None = None,
) -> bool:
    """Move an existing item to the next playable position.

    The active item stays first so moving a song to the top never interrupts
    the song that is already playing (or currently being prepared).
    """
    rows = _ordered_queue_items(session)
    active_item = next(
        (row for row in rows if int(row.id) == int(active_item_id)),
        None,
    ) if active_item_id is not None else None

    if active_item is not None and int(active_item.id) == int(item.id):
        return False

    if active_item is not None:
        active_item.queue_position = 0

    item.queue_position = 1
    other_rows = [
        row
        for row in rows
        if int(row.id) not in {int(item.id), int(active_item.id) if active_item else -1}
    ]
    for position, row in enumerate(other_rows, start=2):
        row.queue_position = position
    return True


def _place_item_next_in_shuffle_queue(item_id: int, active_item_id: int | None) -> None:
    """Make a prioritized request play next even while shuffle is enabled."""
    global _current_shuffle_index, _shuffle_queue

    if not _shuffle_enabled:
        return

    current_shuffle_id = (
        _shuffle_queue[_current_shuffle_index]
        if 0 <= _current_shuffle_index < len(_shuffle_queue)
        else None
    )
    _shuffle_queue[:] = [queued_id for queued_id in _shuffle_queue if queued_id != item_id]
    if current_shuffle_id in _shuffle_queue:
        _current_shuffle_index = _shuffle_queue.index(current_shuffle_id)
    elif _current_shuffle_index >= len(_shuffle_queue):
        _current_shuffle_index = len(_shuffle_queue) - 1

    insert_at: int | None = None
    if active_item_id is not None:
        try:
            insert_at = _shuffle_queue.index(active_item_id) + 1
        except ValueError:
            insert_at = None
    if insert_at is None and 0 <= _current_shuffle_index < len(_shuffle_queue):
        insert_at = _current_shuffle_index + 1
    if insert_at is None:
        insert_at = 0

    _shuffle_queue.insert(insert_at, item_id)
    if current_shuffle_id in _shuffle_queue:
        _current_shuffle_index = _shuffle_queue.index(current_shuffle_id)
    elif insert_at <= _current_shuffle_index:
        _current_shuffle_index += 1


async def _enqueue_qqmusic_song(
    *,
    song_mid: str,
    title: str,
    artist: str,
    play_now: bool,
    requested_by: str,
    prioritize: bool = False,
    quality: str = "320",
    album_mid: str = "",
    album: str = "",
    artwork_url: str = "",
    duration_ms: int | None = None,
) -> tuple[int, bool]:
    """Enqueue a QQ Music song"""
    session = new_session()
    try:
        # Use the server-side QQ Music administrator cookie.
        cookie = _get_admin_qqmusic_cookie(session)
        qqmusic.set_cookie(cookie)

        normalized_quality = _normalize_qqmusic_quality(quality)
        url = ""
        if play_now:
            # Queued QQ Music items obtain a fresh URL just before playback;
            # immediate playback needs one now.
            url = await qqmusic.get_music_url_simple(song_mid, normalized_quality)
            if not url:
                raise HTTPException(status_code=404, detail="无法获取 QQ 音乐播放链接，可能需要 VIP 会员或该歌曲不可用")
        
        # Prefer metadata supplied by the search result; derive a cover when
        # only the album MID is available.
        album_cover_url = str(artwork_url or "").strip()
        if not album_cover_url and album_mid:
            album_cover_url = qqmusic.get_song_cover_image(album_mid)
        resolved_duration_ms = int(duration_ms) if duration_ms is not None and int(duration_ms) > 0 else 0

        active_item_id: int | None = None
        if prioritize:
            async with _playback_lock:
                active_item_id = _current_queue_item_id or _pending_queue_item_id
        queue_position = _queue_position_for_new_item(
            session,
            prioritize=prioritize,
            active_item_id=active_item_id,
        )
        
        # Create queue item
        item = QueueItem(
            track_id=f"qqmusic:{song_mid}",
            queue_position=queue_position,
            title=title,
            artist=artist,
            album=album,
            duration=resolved_duration_ms,
            cover_url=album_cover_url,
            source_url=_encode_qqmusic_queue_source(normalized_quality, url),
        )
        session.add(item)
        session.commit()

        if prioritize:
            _place_item_next_in_shuffle_queue(int(item.id), active_item_id)

        _schedule_ts_description_update()

        if play_now:
            await _set_now_playing_queue_item(
                int(item.id),
                url,
                duration_ms=resolved_duration_ms,
                artist=artist,
                album=album,
                artwork_url=album_cover_url,
            )
            await voice.play(source_url=url, title=title, requested_by=requested_by, notice="")
            hist = HistoryItem(
                track_id=item.track_id,
                title=title,
                artist=artist,
                album=album,
                duration=resolved_duration_ms,
                cover_url=album_cover_url,
                source_url=url,
                requested_by=requested_by,
            )
            session.add(hist)
            session.commit()

        return int(item.id), False  # QQ Music doesn't have trial mode
    finally:
        session.close()


async def _handle_chat_command(
    invoker_name: str,
    message: str,
    *,
    target_mode: int = 2,
    invoker_unique_id: str = "",
) -> None:
    raw = (message or "")
    msg = raw.strip()
    if not msg:
        return

    s = msg
    if s.startswith("!") or s.startswith("！"):
        s = s[1:].lstrip()
    if not s:
        return

    head = s
    tail = ""
    for sep in (" ", "\t", ":", "："):
        idx = s.find(sep)
        if idx != -1:
            head = s[:idx]
            tail = s[idx + 1 :]
            if sep in (":", "："):
                tail = tail.lstrip()
            break

    head_norm = head.strip().lower()
    alias_to_cmd = {
        "help": "help",
        "h": "help",
        "?": "help",
        "帮助": "help",
        "菜单": "help",
        "指令": "help",
        "命令": "help",
        "search": "search",
        "s": "search",
        "find": "search",
        "搜": "search",
        "搜索": "search",
        "查": "search",
        "playlist": "playlist",
        "playlists": "playlist",
        "歌单list": "playlist",
        "歌单": "playlist",
        "歌单列表": "playlist",
        "select": "select",
        "选择": "select",
        "选歌单": "select",
        "歌单选择": "select",
        "clear": "clear",
        "清空": "clear",
        "清空队列": "clear",
        "random": "random",
        "shuffle": "random",
        "随机": "random",
        "随机播放": "random",
        "随机播放列表里的曲目": "random",
        "order": "order",
        "ordered": "order",
        "顺序": "order",
        "顺序播放": "order",
        "顺序播放列表里的曲目": "order",
        "add": "add",
        "a": "add",
        "加": "add",
        "增加": "add",
        "入队": "add",
        "点歌": "add",
        "play": "play",
        "p": "play",
        "播放": "play",
        "来一首": "play",
        "放": "play",
        "vol": "vol",
        "volume": "vol",
        "音量": "vol",
        "声音": "vol",
        "now": "now",
        "np": "now",
        "status": "now",
        "状态": "now",
        "当前": "now",
        "queue": "queue",
        "q": "queue",
        "队列": "queue",
        "列表": "queue",
        "pause": "pause",
        "暂停": "pause",
        "resume": "resume",
        "continue": "resume",
        "恢复": "resume",
        "继续": "resume",
        "stop": "stop",
        "停止": "stop",
        "skip": "skip",
        "next": "skip",
        "跳过": "skip",
        "下一首": "skip",
        "切歌": "skip",
        "desc": "desc",
        "简介": "desc",
        "签名": "desc",
        "fx": "fx",
        "音效": "fx",
    }

    cmd = alias_to_cmd.get(head_norm)
    if not cmd:
        return
    arg = tail.strip()
    prioritize_request = head_norm == "点歌"
    invoker_key = _ts_playlist_result_key(
        invoker_unique_id=invoker_unique_id,
        invoker_name=invoker_name,
    )

    async def reply(text: str) -> None:
        t = (text or "").strip()
        if not t:
            return
        if len(t) > 700:
            t = t[:700] + "..."
        await voice.send_notice(t, target_mode=int(target_mode))

    try:
        if cmd in ("help", "h"):
            await reply(_format_help())
            return

        if cmd in ("now", "np", "status"):
            st = await voice.get_status()
            session = new_session()
            try:
                q_total = int(session.execute(select(func.count(QueueItem.id))).scalar() or 0)
            finally:
                session.close()
            title = (st.now_playing_title or "").strip()
            if title:
                await reply(f"当前: {title}\n状态: {st.state} / 音量: {st.volume_percent} / 队列: {q_total}")
            else:
                await reply(f"当前: (空闲)\n状态: {st.state} / 音量: {st.volume_percent} / 队列: {q_total}")
            return

        if cmd == "queue":
            session = new_session()
            try:
                total = int(session.execute(select(func.count(QueueItem.id))).scalar() or 0)
                rows = _ordered_queue_items(session)[:5]
                if not rows:
                    await reply("队列为空")
                    return
                lines = [f"#{r.id} {r.title} - {r.artist}".strip(" -") for r in rows]
                await reply(f"队列(前{len(lines)}/共{total}):\n" + "\n".join(lines))
                return
            finally:
                session.close()

        if cmd == "clear":
            session = new_session()
            try:
                result = await _clear_queue_internal(session)
            finally:
                session.close()
            await reply(f"已清空播放队列（{result['removed_count']} 首）")
            return

        if cmd in ("random", "order"):
            requested_mode = arg.strip().lower()
            if cmd == "random" and requested_mode in ("off", "0", "false", "关", "关闭"):
                enabled = False
            elif cmd == "order" and requested_mode in ("random", "on", "1", "true", "开"):
                enabled = True
            else:
                enabled = cmd == "random"
            await _set_shuffle_enabled(enabled)
            mode = "随机播放" if enabled else "顺序播放"
            if enabled:
                try:
                    st = await voice.get_status()
                    cur = str(getattr(st, "state", "") or "").strip().upper()
                    if cur == "STATE_IDLE":
                        await _auto_play_next_from_queue()
                except Exception:
                    pass
            await reply(f"已切换为{mode}")
            return

        if cmd == "playlist":
            if not arg:
                await reply("用法: playlist <歌单关键词>")
                return
            raw = await qqmusic.search_with_keyword(arg, search_type=3, result_num=5, page_num=1)
            playlists = _extract_qqmusic_playlist_search_items(raw)
            if not playlists:
                await reply("没有找到 QQ 音乐歌单")
                return
            _remember_ts_playlist_results(invoker_key, playlists)
            lines: list[str] = []
            for index, playlist in enumerate(playlists, start=1):
                detail = f"{playlist['name']}"
                creator = playlist.get("creator") or ""
                track_count = playlist.get("track_count") or ""
                suffix = ""
                if creator:
                    suffix += f" - {creator}"
                if track_count:
                    suffix += f" ({track_count}首)"
                lines.append(f"{index}. {detail}{suffix} [id={playlist['id']}]")
            await reply("QQ 音乐歌单搜索结果（使用 select <编号> 加入队列）：\n" + "\n".join(lines))
            return

        if cmd == "select":
            if not arg:
                await reply("用法: select <歌单编号>")
                return
            playlists = _get_ts_playlist_results(invoker_key)
            selection_token = arg.split()[0].strip()
            selected_playlist: dict[str, str] | None = None
            try:
                selection = int(selection_token)
            except ValueError:
                selection = -1
            if 1 <= selection <= len(playlists):
                selected_playlist = playlists[selection - 1]
            else:
                selected_playlist = next(
                    (playlist for playlist in playlists if playlist.get("id") == selection_token),
                    None,
                )
            if selected_playlist is None:
                await reply("歌单编号无效，请先使用 playlist 搜索")
                return
            playlist_id = selected_playlist["id"]

            playlist_name, tracks = await _load_qqmusic_playlist_tracks(playlist_id)
            normalized_tracks = [_normalize_qqmusic_song(track) for track in tracks]
            normalized_tracks = [track for track in normalized_tracks if track is not None]
            if not normalized_tracks:
                await reply("歌单为空，或 QQ 音乐未返回可用歌曲")
                return

            added = 0
            failed = 0
            for normalized in normalized_tracks:
                try:
                    await _enqueue_qqmusic_song(
                        song_mid=str(normalized["song_mid"]),
                        title=str(normalized["title"]),
                        artist=str(normalized["artist"]),
                        play_now=False,
                        requested_by=invoker_name,
                        quality="320",
                        album_mid=str(normalized.get("album_mid") or ""),
                        album=str(normalized.get("album") or ""),
                        artwork_url=str(normalized.get("artwork_url") or ""),
                        duration_ms=normalized.get("duration_ms"),
                    )
                    added += 1
                except Exception:
                    failed += 1

            auto_started = False
            if added:
                try:
                    st = await voice.get_status()
                    cur = str(getattr(st, "state", "") or "").strip().upper()
                    if cur == "STATE_IDLE":
                        await _auto_play_next_from_queue()
                        auto_started = True
                except Exception:
                    pass
            label = playlist_name or selected_playlist.get("name") or playlist_id
            status = f"已从歌单《{label}》加入 {added} 首歌曲"
            if failed:
                status += f"，{failed} 首失败"
            if auto_started:
                status += "，已开始播放"
            await reply(status)
            return

        if cmd == "pause":
            await _mark_playback_paused()
            await voice.pause()
            await reply("已暂停")
            return

        if cmd in ("resume", "continue"):
            await _mark_playback_resumed()
            await voice.resume()
            await reply("已恢复")
            return

        if cmd == "stop":
            await _invalidate_play_requests()
            await _set_now_playing_queue_item(None)
            await voice.stop()
            await reply("已停止")
            return

        if cmd == "skip":
            # Get current playing item to remove it
            current_item_id = None
            pending_item_id = None
            async with _playback_lock:
                current_item_id = _current_queue_item_id
                pending_item_id = _pending_queue_item_id
            active_item_id = current_item_id or pending_item_id
            
            if active_item_id:
                # Remove current song from queue
                await _remove_queue_item_internal(active_item_id)
                await _invalidate_play_requests()
                
                # Stop current playback
                await _set_now_playing_queue_item(None)
                await voice.skip()
                
                # Auto play next song
                await _auto_play_next_from_queue(start_after_id=active_item_id)
                await reply("已跳过当前歌曲并播放下一首")
            else:
                await _invalidate_play_requests()
                await reply("当前没有正在播放的歌曲")
            return

        if cmd in ("vol", "volume"):
            if not arg:
                await reply("用法: vol <0-200>")
                return
            try:
                v = int(arg)
            except ValueError:
                await reply("用法: vol <0-200>")
                return
            v = max(0, min(200, v))
            await voice.set_volume(v)
            session = new_session()
            try:
                row = session.get(Secret, "voice_volume")
                if not row:
                    row = Secret(key="voice_volume", value=str(v))
                    session.add(row)
                else:
                    row.value = str(v)
                session.commit()
            finally:
                session.close()
            await reply(f"音量已设置为 {v}")
            return

        if cmd == "fx":
            if not arg:
                fx = await voice.get_audio_fx()
                await reply(
                    f"音效: pan={fx.pan:.2f} width={fx.width:.2f} swap_lr={int(fx.swap_lr)} bass_db={fx.bass_db:.1f} reverb_mix={fx.reverb_mix:.2f}\n"
                    "用法: fx pan <-1..1> | fx width <0..3> | fx swap <on|off> | fx bass <0..18> | fx reverb <0..1> | fx reset"
                )
                return

            parts = [p for p in arg.split() if p]
            sub = (parts[0] if parts else "").strip().lower()

            if sub == "reset":
                await voice.set_audio_fx(pan=0.0, width=1.0, swap_lr=False, bass_db=0.0, reverb_mix=0.0)
                fx = await voice.get_audio_fx()
                await reply(
                    f"已重置音效: pan={fx.pan:.2f} width={fx.width:.2f} swap_lr={int(fx.swap_lr)} bass_db={fx.bass_db:.1f} reverb_mix={fx.reverb_mix:.2f}"
                )
                return

            if len(parts) < 2:
                await reply("用法: fx pan <-1..1> | fx width <0..3> | fx swap <on|off> | fx bass <0..18> | fx reverb <0..1> | fx reset")
                return

            val = parts[1].strip().lower()
            if sub == "pan":
                try:
                    p = float(val)
                except ValueError:
                    await reply("用法: fx pan <-1..1>")
                    return
                await voice.set_audio_fx(pan=max(-1.0, min(1.0, p)))
            elif sub == "width":
                try:
                    w = float(val)
                except ValueError:
                    await reply("用法: fx width <0..3>")
                    return
                await voice.set_audio_fx(width=max(0.0, min(3.0, w)))
            elif sub == "swap":
                on = val in ("1", "true", "on", "yes", "y", "开")
                off = val in ("0", "false", "off", "no", "n", "关")
                if not (on or off):
                    await reply("用法: fx swap <on|off>")
                    return
                await voice.set_audio_fx(swap_lr=bool(on))
            elif sub == "bass":
                try:
                    b = float(val)
                except ValueError:
                    await reply("用法: fx bass <0..18>")
                    return
                await voice.set_audio_fx(bass_db=max(0.0, min(18.0, b)))
            elif sub == "reverb":
                try:
                    m = float(val)
                except ValueError:
                    await reply("用法: fx reverb <0..1>")
                    return
                await voice.set_audio_fx(reverb_mix=max(0.0, min(1.0, m)))
            else:
                await reply("用法: fx pan <-1..1> | fx width <0..3> | fx swap <on|off> | fx bass <0..18> | fx reverb <0..1> | fx reset")
                return

            fx = await voice.get_audio_fx()
            await reply(
                f"音效已更新: pan={fx.pan:.2f} width={fx.width:.2f} swap_lr={int(fx.swap_lr)} bass_db={fx.bass_db:.1f} reverb_mix={fx.reverb_mix:.2f}"
            )
            return

        if cmd == "desc":
            if not arg:
                await reply("用法: desc <内容>")
                return
            await voice.set_client_description(arg)
            await reply("简介已更新")
            return

        if cmd == "search":
            if not arg:
                await reply("用法: search <关键词>")
                return
            songs = _normalize_qqmusic_search_items(
                await qqmusic.search_songs_simple(arg, limit=5, page=1)
            )
            if not songs:
                await reply("没有找到 QQ 音乐结果")
                return
            lines: list[str] = []
            for i, song in enumerate(songs[:5], start=1):
                lines.append(
                    f"{i}. {song['song_mid']} {song['title']} - {song['artist']}".strip(" -")
                )
            await reply("QQ 音乐搜索结果（可直接用 点歌/add/play + song_mid）：\n" + "\n".join(lines))
            return

        if cmd == "play" and not arg:
            session = new_session()
            first_item_id: int | None = None
            first_title = ""
            first_artist = ""
            try:
                first_item = session.execute(
                    select(QueueItem).order_by(*_queue_order_by()).limit(1)
                ).scalars().first()
                if first_item is not None:
                    first_item_id = int(first_item.id)
                    first_title = str(first_item.title or "").strip()
                    first_artist = str(first_item.artist or "").strip()
            finally:
                session.close()

            if first_item_id is None:
                await reply("播放队列为空")
                return
            try:
                ok = await _play_queue_item_internal(first_item_id, requested_by=invoker_name or "ts")
            except HTTPException as exc:
                if _should_auto_skip_unplayable_queue_item(exc):
                    await _skip_unplayable_queue_item(first_item_id, reason=str(exc.detail or ""))
                    await reply("队首歌曲无法播放，已跳过并尝试播放下一首")
                    return
                raise
            if not ok:
                await _skip_unplayable_queue_item(first_item_id, reason="队列歌曲已不存在或不可播放")
                await reply("队首歌曲无法播放，已跳过并尝试播放下一首")
                return
            label = f"{first_title} - {first_artist}".strip(" -")
            await reply(f"已播放队列第一首: {label}")
            return

        if cmd in ("add", "play"):
            if not arg:
                await reply(f"用法: {cmd} <song_mid|关键词>")
                return
            song_mid = _try_parse_qqmusic_song_mid(arg)
            title = ""
            artist = ""
            album = ""
            album_mid = ""
            artwork_url = ""
            duration_ms: int | None = None

            if song_mid is None:
                songs = _normalize_qqmusic_search_items(
                    await qqmusic.search_songs_simple(arg, limit=1, page=1)
                )
                if not songs:
                    await reply("没有找到 QQ 音乐结果")
                    return
                first = songs[0]
                song_mid = str(first["song_mid"])
                title = str(first.get("title") or song_mid)
                artist = str(first.get("artist") or "")
                album = str(first.get("album") or "")
                album_mid = str(first.get("album_mid") or "")
                artwork_url = str(first.get("artwork_url") or "")
                duration_ms = first.get("duration_ms")
            else:
                title = song_mid

            item_id, trial = await _enqueue_qqmusic_song(
                song_mid=song_mid,
                title=title,
                artist=artist,
                play_now=(cmd == "play"),
                requested_by=invoker_name,
                prioritize=prioritize_request,
                quality="320",
                album_mid=album_mid,
                album=album,
                artwork_url=artwork_url,
                duration_ms=duration_ms,
            )
            song_label = f"{title} - {artist}".strip(" -")
            extra = ""
            if trial:
                extra = "(试听)"
            if cmd == "play":
                await reply(f"立即播放: #{item_id} {song_label} {extra}\n点歌: {invoker_name}")
                return

            auto_started = False
            try:
                st = await voice.get_status()
                cur = str(getattr(st, "state", "") or "").strip().upper()
                if cur == "STATE_IDLE":
                    await _auto_play_next_from_queue()
                    auto_started = True
            except Exception as e:
                action = "已置顶" if prioritize_request else "已加入队列"
                await reply(f"{action}: #{item_id} {song_label} {extra}\n点歌: {invoker_name}\n自动播放失败: {e}")
                return

            if auto_started:
                await reply(f"已置顶并开始播放: #{item_id} {song_label} {extra}\n点歌: {invoker_name}")
            elif prioritize_request:
                await reply(f"已置顶，将在下一首播放: #{item_id} {song_label} {extra}\n点歌: {invoker_name}")
            else:
                await reply(f"已加入队列: #{item_id} {song_label} {extra}\n点歌: {invoker_name}")

            return

        await reply("unknown command, try !help")
    except HTTPException as e:
        detail = str(getattr(e, "detail", "") or "").strip()
        if detail == "admin qqmusic cookie not set":
            await reply("QQ 音乐后台授权未配置，请在 Web 控制台“系统设置 → 音乐会员登录”中完成 QQ 音乐后台授权")
            return
        if detail == "failed to decrypt admin qqmusic cookie":
            await reply("QQ 音乐后台授权无法解密，请检查 TSBOT_COOKIE_KEY 是否与保存授权时一致")
            return
        if e.status_code == 404:
            await reply("加载失败：歌曲不存在/已下架（无版权或资源不可用）")
            return
        if e.status_code == 402:
            await reply("加载失败：需要 VIP/付费账号（已尝试试听/降码率，如仍失败请换歌）")
            return
        if e.status_code == 403:
            await reply("加载失败：无版权/地区限制/不可播放")
            return
        if detail:
            await reply(f"error: {e.status_code}: {detail}")
        else:
            await reply(f"error: {e.status_code}")
    except Exception as e:
        await reply(f"error: {e}")


async def _handle_playback_finished(source_url: str) -> None:
    item_id = await _take_now_playing_if_match(source_url=source_url)
    if item_id is None:
        return
    if _repeat_mode == "one":
        try:
            replayed = await _play_queue_item_internal(item_id, requested_by="auto")
        except HTTPException as exc:
            if _should_auto_skip_unplayable_queue_item(exc):
                await _skip_unplayable_queue_item(item_id, reason=str(exc.detail or ""))
                return
            raise
        if not replayed:
            await _skip_unplayable_queue_item(item_id, reason="队列歌曲已不存在或不可播放")
        return
    await _delete_queue_item(item_id)
    await _auto_play_next_from_queue()


async def _chat_command_worker() -> None:
    retry_delay = 1.0
    while True:
        try:
            async for ev in voice.subscribe_events(include_chat=True, include_playback=True, include_log=False):
                retry_delay = 1.0
                try:
                    if not hasattr(ev, "WhichOneof"):
                        continue
                    kind = ev.WhichOneof("payload")

                    if kind == "chat":
                        chat = ev.chat
                        try:
                            logger.info(
                                "ts3 chat event: target_mode=%s invoker=%s msg=%s",
                                int(getattr(chat, "target_mode", 0) or 0),
                                str(getattr(chat, "invoker_name", "") or ""),
                                str(getattr(chat, "message", "") or ""),
                            )
                        except Exception:
                            pass
                        await _handle_chat_command(
                            str(getattr(chat, "invoker_name", "")),
                            str(getattr(chat, "message", "")),
                            target_mode=int(getattr(chat, "target_mode", 2) or 2),
                            invoker_unique_id=str(getattr(chat, "invoker_unique_id", "") or ""),
                        )
                        continue

                    if kind == "playback":
                        pb = ev.playback
                        ty = int(getattr(pb, "type", 0) or 0)
                        src = str(getattr(pb, "source_url", "") or "")
                        # PlaybackEvent.Type: STARTED=1, FINISHED=2, ERROR=3
                        if ty == 2:
                            await _handle_playback_finished(src)
                        if ty == 3:
                            item_id = await _take_now_playing_if_match(source_url=src)
                            if item_id is not None:
                                await _skip_unplayable_queue_item(
                                    item_id,
                                    reason=str(getattr(pb, "detail", "") or "播放过程发生错误"),
                                )
                        continue
                except Exception:
                    logger.exception("chat worker: failed to handle event")
                    continue
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning(
                "voice 事件订阅暂时中断（%s），%.0f 秒后重连",
                type(exc).__name__,
                retry_delay,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(30.0, retry_delay * 2)


def _set_secret(session: Session, key: str, plaintext: str) -> None:
    row = session.get(Secret, key)
    enc = encrypt_text(plaintext)
    if not row:
        row = Secret(key=key, value=enc)
        session.add(row)
    else:
        row.value = enc
    session.commit()


@app.get("/config/public")
def public_config() -> dict:
    icon = ASSET_BY_KEY["web-app-icon"]
    return {
        "app_name": settings.web_app_name,
        "app_icon": icon.public_path if asset_path(icon).is_file() else "",
        "log_level": settings.web_log_level,
    }


@app.get("/admin/version")
def admin_version(request: Request) -> dict:
    _require_admin_token(request)
    return build_info()


@app.get("/admin/update/check")
async def admin_check_update(request: Request) -> dict:
    _require_admin_token(request)
    try:
        latest = await latest_release()
    except Exception as exc:
        logger.warning("检查更新失败，异常类型 %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="检查更新失败，请检查服务器网络后重试") from None
    current = build_info()
    current_version = parse_version(current["version"])
    latest_version = parse_version(latest["version"])
    return {"current": current, "latest": latest, "update_available": current_version < latest_version if current_version and latest_version else None}


@app.get("/admin/update/status")
def admin_update_status(request: Request) -> dict:
    _require_admin_token(request)
    return update_status()


class SoftwareUpdateRequest(BaseModel):
    version: str


@app.post("/admin/update")
async def admin_start_update(req: SoftwareUpdateRequest, request: Request) -> dict:
    _require_admin_token(request)
    if not update_status()["available"]:
        raise HTTPException(status_code=409, detail="宿主机更新服务尚未安装")
    try:
        release = await latest_release()
    except Exception as exc:
        logger.warning("启动更新前检查版本失败，异常类型 %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="无法检查最新版本，请稍后重试") from None
    current = parse_version(build_info()["version"])
    target = parse_version(release["version"])
    if req.version != release["version"]:
        raise HTTPException(status_code=409, detail="最新版本已变化，请重新检查更新")
    if current is None or target is None or target <= current:
        raise HTTPException(status_code=409, detail="当前版本无法比较或已是最新版本")
    try:
        submit_update(release["version"])
    except (FileExistsError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except OSError as exc:
        logger.warning("提交更新请求失败，异常类型 %s", type(exc).__name__)
        raise HTTPException(status_code=500, detail="无法提交更新请求，请检查宿主机更新服务") from None
    return {"ok": True, "version": release["version"]}


@app.get("/assets/{asset_key}")
def managed_asset_file(asset_key: str) -> FileResponse:
    asset = ASSET_BY_KEY.get(asset_key)
    if asset is None or not asset.public_path:
        raise HTTPException(status_code=404, detail="未知图片资源")
    path = asset_path(asset)
    if not path.is_file():
        raise HTTPException(status_code=404, detail="尚未上传图片")
    media_type = detect_image_type(path.read_bytes()) or "application/octet-stream"
    return FileResponse(path, media_type=media_type, headers={"Cache-Control": "no-cache"})


@app.get("/auth/status")
def auth_status(request: Request, session: Session = Depends(get_session)) -> dict:
    credential = session.get(AdminCredential, 1)
    payload = {
        "initialized": credential is not None,
        "authenticated": False,
        "must_change_password": False,
        "username": "",
        "csrf_token": "",
    }
    if credential is None:
        return payload
    try:
        credential, admin_session = get_admin_session(request, session)
    except HTTPException:
        return payload
    payload.update({
        "authenticated": True,
        "must_change_password": credential.must_change_password,
        "username": credential.username,
        "csrf_token": admin_session.csrf_token,
    })
    return payload


@app.post("/auth/login")
def auth_login(
    req: AdminLoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> dict:
    _check_login_rate_limit(request)
    credential = session.get(AdminCredential, 1)
    username_ok = credential is not None and hmac.compare_digest(req.username.strip(), credential.username)
    password_ok = credential is not None and verify_password(req.password, credential.password_hash)
    if not username_ok or not password_ok or credential is None:
        _record_login_failure(request)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    _login_attempts.pop(_login_rate_limit_key(request), None)
    raw_token, admin_session = create_session(session, credential)
    set_session_cookie(response, raw_token, request)
    return {
        "authenticated": True,
        "must_change_password": credential.must_change_password,
        "username": credential.username,
        "csrf_token": admin_session.csrf_token,
    }


@app.post("/auth/change-password")
def auth_change_password(
    req: AdminPasswordChangeRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> dict:
    credential, admin_session = require_admin(request, session, allow_password_change=True)
    require_csrf(request, admin_session)
    if not verify_password(req.current_password, credential.password_hash):
        raise HTTPException(status_code=400, detail="当前密码错误")
    new_password = req.new_password
    if len(new_password) < 10:
        raise HTTPException(status_code=422, detail="新密码至少需要 10 个字符")
    if hmac.compare_digest(req.current_password, new_password):
        raise HTTPException(status_code=422, detail="新密码不能与当前密码相同")
    credential.password_hash = hash_password(new_password)
    credential.must_change_password = False
    credential.password_version += 1
    session.commit()
    invalidate_sessions(session)
    raw_token, new_session_row = create_session(session, credential)
    set_session_cookie(response, raw_token, request)
    remove_initial_password_file()
    return {
        "authenticated": True,
        "must_change_password": False,
        "username": credential.username,
        "csrf_token": new_session_row.csrf_token,
    }


@app.post("/auth/logout")
def auth_logout(request: Request, response: Response, session: Session = Depends(get_session)) -> dict:
    _, admin_session = require_admin(request, session, allow_password_change=True)
    require_csrf(request, admin_session)
    session.delete(admin_session)
    session.commit()
    clear_session_cookie(response)
    return {"ok": True}


@app.get("/admin/settings")
def admin_settings(request: Request, session: Session = Depends(get_session)) -> dict:
    require_admin(request, session)
    return settings_payload(session)


@app.get("/admin/cache")
def admin_cache_status(request: Request, session: Session = Depends(get_session)) -> dict:
    require_admin(request, session)
    return _cache_status_payload()


def _cache_status_payload() -> dict:
    """Return the stable cache API shape after removing local media caching."""
    return {
        "cache": {"size_bytes": 0, "file_count": 0},
        "memory_entries": {},
        "total_size_bytes": 0,
        "total_file_count": 0,
    }


def _log_line_limit(lines: int) -> int:
    return max(_LOG_VIEW_MIN_LINES, min(int(lines), _LOG_VIEW_MAX_LINES))


def _runtime_path(value: object, default: str) -> Path:
    path = Path(str(value or default).strip() or default)
    if not path.is_absolute():
        path = Path(__file__).resolve().parent.parent / path
    return path


def _voice_log_file_path() -> Path:
    return _runtime_path(os.getenv("TSBOT_VOICE_LOG_FILE", "./logs/voice.log"), "./logs/voice.log")


def _tail_log_file(path: Path, *, lines: int) -> str:
    """Read the final log lines while bounding memory use for large files."""
    try:
        if not path.is_file():
            return ""
        with path.open("rb") as file:
            file.seek(0, os.SEEK_END)
            size = file.tell()
            start = max(0, size - _LOG_VIEW_MAX_BYTES)
            file.seek(start)
            data = file.read()
    except OSError:
        return ""

    if start:
        first_newline = data.find(b"\n")
        if first_newline >= 0:
            data = data[first_newline + 1:]
    text = data.decode("utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def _redact_log_secrets(content: str) -> str:
    """Avoid exposing accidental credentials through the administrator log UI."""
    redacted = _LOG_SECRET_ASSIGNMENT_RE.sub(r"\1\2[已隐藏]", content)
    redacted = _INITIAL_PASSWORD_RE.sub(r"\1[已隐藏]", redacted)
    return _BEARER_TOKEN_RE.sub(r"\1[已隐藏]", redacted)


def _bot_log_payload(lines: int) -> dict:
    sources = (
        ("后端", _runtime_path(settings.log_file, "./logs/backend.log")),
        ("Voice", _voice_log_file_path()),
    )
    sections: list[str] = []
    missing_sources: list[str] = []
    line_count = 0
    for label, path in sources:
        content = _tail_log_file(path, lines=lines)
        if content:
            sections.append(f"===== {label} =====\n{_redact_log_secrets(content)}")
            line_count += len(content.splitlines())
        else:
            missing_sources.append(label)

    message = ""
    if missing_sources:
        labels = "、".join(missing_sources)
        message = f"{labels} 日志文件尚未生成或暂时没有内容。"
    return {
        "source": "bot",
        "title": "机器人日志",
        "content": "\n\n".join(sections),
        "line_count": line_count,
        "message": message,
    }


def _serverquery_log_config(session: Session) -> ServerQueryConfig:
    def value(key: str) -> str:
        return str(get_value(session, DEFINITION_BY_KEY[key]) or "").strip()

    username = value("voice.serverquery_user")
    password = value("voice.serverquery_password")
    if not username or not password:
        raise HTTPException(status_code=400, detail="请先在 ServerQuery 兼容中配置用户名和密码")

    host = value("voice.serverquery_host") or value("voice.ts3_host") or "127.0.0.1"
    try:
        port = int(value("voice.serverquery_port") or "10011")
        virtual_server_port = int(value("voice.serverquery_use_port") or value("voice.ts3_port") or "9987")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="ServerQuery 端口配置无效") from exc
    if not 1 <= port <= 65535 or not 1 <= virtual_server_port <= 65535:
        raise HTTPException(status_code=400, detail="ServerQuery 端口配置无效")

    return ServerQueryConfig(
        host=host,
        port=port,
        username=username,
        password=password,
        virtual_server_sid=value("voice.serverquery_sid"),
        virtual_server_port=virtual_server_port,
    )


@app.get("/admin/logs/{source}")
async def admin_logs(
    source: str,
    request: Request,
    lines: int = 200,
    session: Session = Depends(get_session),
) -> dict:
    """Read recent bot logs or TeamSpeak server logs for an authenticated admin."""
    require_admin(request, session)
    line_limit = _log_line_limit(lines)
    normalized_source = (source or "").strip().lower()

    if normalized_source == "bot":
        return _bot_log_payload(line_limit)
    if normalized_source != "teamspeak":
        raise HTTPException(status_code=404, detail="未知日志来源")

    try:
        entries = await query_server_logs(_serverquery_log_config(session), lines=line_limit)
    except ServerQueryError as exc:
        logger.warning("读取 TeamSpeak 服务器日志失败: %s", exc)
        raise HTTPException(status_code=502, detail=f"无法读取 TeamSpeak 服务器日志：{exc}") from exc

    return {
        "source": "teamspeak",
        "title": "TeamSpeak 服务器日志",
        "content": _redact_log_secrets("\n".join(entries)),
        "line_count": len(entries),
        "message": "" if entries else "没有可显示的 TeamSpeak 服务器日志。",
    }


@app.delete("/admin/cache")
def admin_clear_cache(request: Request, session: Session = Depends(get_session)) -> dict:
    _, admin_session = require_admin(request, session)
    require_csrf(request, admin_session)
    return {
        "ok": True,
        "removed_files": 0,
        "removed_bytes": 0,
        "skipped_files": 0,
        "skipped_bytes": 0,
        "cleared_memory_entries": 0,
        **_cache_status_payload(),
    }


@app.put("/admin/settings")
async def admin_update_settings(
    req: SettingsUpdateRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    _, admin_session = require_admin(request, session)
    require_csrf(request, admin_session)
    effects = update_settings(session, req.values, apply=req.apply)
    if req.apply:
        if "backend.voice_grpc_addr" in req.values:
            await voice.close()
        if "voice" in effects and any(key.startswith("voice.description_") for key in req.values):
            _schedule_ts_description_update()
    return {
        "ok": True,
        "voice_restart_requested": req.apply and "voice" in effects,
        "voice_config_revision": voice_config_revision() if req.apply and "voice" in effects else "",
        "backend_restart_required": req.apply and "backend" in effects,
        **settings_payload(session),
    }


async def _read_managed_asset(request: Request) -> bytes:
    content_length = request.headers.get("content-length", "").strip()
    if content_length:
        try:
            if int(content_length) > MAX_IMAGE_BYTES:
                raise HTTPException(status_code=413, detail="图片不能超过 5 MiB")
        except ValueError:
            raise HTTPException(status_code=400, detail="无效的 Content-Length") from None
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content) > MAX_IMAGE_BYTES:
            raise HTTPException(status_code=413, detail="图片不能超过 5 MiB")
    return bytes(content)


@app.put("/admin/assets/{asset_key}")
async def admin_upload_asset(
    asset_key: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    _, admin_session = require_admin(request, session)
    require_csrf(request, admin_session)
    asset = ASSET_BY_KEY.get(asset_key)
    if asset is None:
        raise HTTPException(status_code=404, detail="未知图片配置")
    content = await _read_managed_asset(request)
    try:
        save_asset(asset, content)
    except ValueError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except OSError as exc:
        logger.error("无法写入固定图片目录 %s: %s", asset_path(asset).parent, exc)
        raise HTTPException(status_code=500, detail="无法写入固定图片目录，请检查数据目录权限") from exc
    revision = write_voice_config(session, force_restart=True) if asset.restart == "voice" else ""
    return {
        "ok": True,
        "voice_restart_requested": asset.restart == "voice",
        "voice_config_revision": revision,
        "asset": asset_payload(asset),
    }


@app.delete("/admin/assets/{asset_key}")
def admin_delete_asset(
    asset_key: str,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    _, admin_session = require_admin(request, session)
    require_csrf(request, admin_session)
    asset = ASSET_BY_KEY.get(asset_key)
    if asset is None:
        raise HTTPException(status_code=404, detail="未知图片配置")
    changed = delete_asset(asset)
    revision = write_voice_config(session, force_restart=True) if changed and asset.restart == "voice" else ""
    return {
        "ok": True,
        "voice_restart_requested": changed and asset.restart == "voice",
        "voice_config_revision": revision,
        "asset": asset_payload(asset),
    }


@app.post("/admin/ts/description")
async def admin_ts_description(req: TSClientDescriptionRequest, request: Request) -> dict:
    _require_admin_token(request)
    desc = (req.description or "").strip()
    if len(desc) > 700:
        raise HTTPException(status_code=400, detail="description too long")
    await voice.set_client_description(desc)
    return {"ok": True}


@app.get("/admin/debug/runtime")
async def admin_debug_runtime(request: Request) -> dict:
    _require_admin_token(request)
    sqlite_db_path = get_sqlite_db_path()
    return {
        "cwd": os.getcwd(),
        "sqlite_db_path": str(Path(sqlite_db_path).resolve()) if sqlite_db_path else None,
        "database_url": get_database_url(),
    }


@app.post("/queue/qqmusic")
async def add_queue_qqmusic(req: AddQQMusicQueueRequest) -> dict:
    try:
        item_id, trial = await _enqueue_qqmusic_song(
            song_mid=req.song_mid,
            title=req.title,
            artist=req.artist,
            play_now=req.play_now,
            requested_by="web",
            quality=req.quality,
            album_mid=req.album_mid,
            album=req.album,
            artwork_url=req.cover_url,
            duration_ms=req.duration_ms,
        )
        return {"ok": True, "id": item_id, "trial": trial}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to enqueue qqmusic song {req.song_mid}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/queue")
def get_queue(session: Session = Depends(get_session)) -> list[dict]:
    rows = _ordered_queue_items(session)
    return [_serialize_queue_item(row) for row in rows]


@app.delete("/queue")
async def clear_queue(session: Session = Depends(get_session)) -> dict:
    return await _clear_queue_internal(session)


@app.post("/queue")
def add_queue(req: AddQueueRequest, session: Session = Depends(get_session)) -> dict:
    item = QueueItem(
        track_id=req.track_id,
        queue_position=_queue_position_for_new_item(session, prioritize=False),
        title=req.title,
        artist=req.artist,
        source_url=req.source_url,
    )
    session.add(item)
    session.commit()
    return {"ok": True, "id": item.id}


@app.delete("/queue/{item_id}")
def delete_queue_item(item_id: int, session: Session = Depends(get_session)) -> dict:
    global _shuffle_queue, _current_shuffle_index

    item = session.get(QueueItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="not found")
    session.delete(item)
    session.commit()

    if _shuffle_enabled and item_id in _shuffle_queue:
        removed_index = _shuffle_queue.index(item_id)
        _shuffle_queue.remove(item_id)
        if removed_index <= _current_shuffle_index:
            _current_shuffle_index = max(-1, _current_shuffle_index - 1)

    _schedule_ts_description_update()
    return {"ok": True}


@app.post("/queue/{item_id}/prioritize")
async def prioritize_queue_item(item_id: int, session: Session = Depends(get_session)) -> dict:
    """Move a queued song to play next without interrupting playback."""
    item = session.get(QueueItem, item_id)
    if not item:
        raise HTTPException(status_code=404, detail="queue item not found")

    async with _playback_lock:
        active_item_id = _current_queue_item_id or _pending_queue_item_id

    moved = _prioritize_existing_queue_item(
        session,
        item,
        active_item_id=active_item_id,
    )
    if not moved:
        return {"ok": True, "id": item_id, "action": "already_current"}

    session.commit()
    _place_item_next_in_shuffle_queue(item_id, active_item_id)
    _schedule_ts_description_update()
    return {"ok": True, "id": item_id, "action": "prioritized"}


@app.post("/queue/{item_id}/play")
async def play_queue_item(item_id: int, session: Session = Depends(get_session)) -> dict:
    try:
        ok = await _play_queue_item_internal(item_id, requested_by="web")
    except HTTPException as exc:
        if _should_auto_skip_unplayable_queue_item(exc):
            await _skip_unplayable_queue_item(item_id, reason=str(exc.detail or ""))
            return {"ok": True, "action": "skipped_unplayable", "skipped_item_id": item_id}
        raise
    if not ok:
        if session.get(QueueItem, item_id) is None:
            raise HTTPException(status_code=404, detail="queue item not found")
        await _skip_unplayable_queue_item(item_id, reason="队列歌曲已不存在或不可播放")
        return {"ok": True, "action": "skipped_unplayable", "skipped_item_id": item_id}

    _schedule_ts_description_update()
    return {"ok": True}


@app.get("/history")
def history(session: Session = Depends(get_session)) -> list[dict]:
    rows = session.execute(select(HistoryItem).order_by(HistoryItem.id.desc()).limit(200)).scalars().all()
    return [_serialize_history_item(row) for row in rows]


async def _replay_history_item(
    hist_item: HistoryItem,
    *,
    play_now: bool,
    requested_by: str,
) -> dict:
    track_id = str(hist_item.track_id or "").strip()
    if not track_id:
        raise HTTPException(status_code=400, detail="history track_id is empty")

    if track_id.startswith("qqmusic:"):
        song_mid = track_id.split(":", 1)[1].strip()
        if not song_mid:
            raise HTTPException(status_code=400, detail="qqmusic song_mid is empty")

        item_id, trial = await _enqueue_qqmusic_song(
            song_mid=song_mid,
            title=hist_item.title,
            artist=str(hist_item.artist or ""),
            play_now=play_now,
            requested_by=requested_by,
            quality="320",
            album_mid="",
            album=str(hist_item.album or ""),
            artwork_url=str(hist_item.cover_url or ""),
            duration_ms=hist_item.duration,
        )
        return {
            "ok": True,
            "source": "qqmusic",
            "queue_id": item_id,
            "trial": trial,
            "play_now": play_now,
            "message": f"{'Playing' if play_now else 'Added to queue'}: {hist_item.title}",
            "track": {
                "source": "qqmusic",
                "track_id": f"qqmusic:{song_mid}",
                "song_mid": song_mid,
            },
        }

    raise HTTPException(status_code=400, detail=f"unsupported history source: {track_id}")


@app.post("/history/{history_id}/replay")
async def replay_from_history(
    history_id: int,
    play_now: bool = True,
    session: Session = Depends(get_session)
) -> dict:
    """Replay a track from history using its track_id to get a fresh playable source"""
    hist_item = session.get(HistoryItem, history_id)
    if not hist_item:
        raise HTTPException(status_code=404, detail="History item not found")

    try:
        return await _replay_history_item(hist_item, play_now=play_now, requested_by="web_history")
    except HTTPException as e:
        if e.status_code in (402, 403):
            raise HTTPException(
                status_code=e.status_code,
                detail=f"Cannot replay '{hist_item.title}': {e.detail}"
            )
        raise


@app.get("/external/status", tags=["External API"])
async def external_status(session: Session = Depends(get_session)) -> dict:
    status = await voice_status()
    queue_items = get_queue(session=session)
    status["queue_length"] = len(queue_items)
    status["queue_preview"] = queue_items[:10]
    return status


@app.post("/external/player/action", tags=["External API"])
async def external_player_action(req: ExternalPlayerActionRequest) -> dict:
    action_aliases = {
        "resume": "play",
        "continue": "play",
        "switch": "next",
    }
    action = action_aliases.get((req.action or "").strip().lower(), (req.action or "").strip().lower())
    if action == "play":
        return await voice_play()
    if action == "pause":
        return await voice_pause()
    if action == "next":
        return await voice_next()
    if action == "previous":
        return await voice_previous()
    if action == "skip":
        return await voice_skip()
    raise HTTPException(status_code=400, detail="unsupported action")


@app.put("/external/player/volume", tags=["External API"])
async def external_set_player_volume(
    req: VolumeUpdateRequest,
    session: Session = Depends(get_session),
) -> dict:
    return await set_voice_volume(req, session=session)


@app.post("/external/player/shuffle", tags=["External API"])
async def external_set_player_shuffle(req: ShuffleRequest) -> dict:
    return await voice_shuffle(req)


@app.post("/external/player/repeat", tags=["External API"])
async def external_set_player_repeat(req: RepeatRequest) -> dict:
    return await voice_repeat(req)


@app.get("/external/search", tags=["External API"])
async def external_search(
    keywords: str,
    source: str = "qqmusic",
    limit: int = 20,
    page: int = 1,
) -> dict:
    query = (keywords or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="keywords is empty")

    provider = (str(source or "").strip().lower() or "qqmusic")
    page = max(1, int(page))
    limit = max(1, min(int(limit), 50))

    if provider == "qqmusic":
        songs = await qqmusic.search_songs_simple(query, limit=limit, page=page)
        items = _normalize_qqmusic_search_items(songs)
        return {
            "source": provider,
            "keywords": query,
            "page": page,
            "limit": limit,
            "has_more": len(items) == limit,
            "items": items,
        }

    raise HTTPException(status_code=400, detail="unsupported source")


@app.post("/external/queue", tags=["External API"])
async def external_add_queue(req: ExternalQueueRequest) -> dict:
    provider = (str(req.source or "").strip().lower() or "qqmusic")
    keywords = (req.keywords or "").strip()
    play_now = bool(req.play_now)

    if provider == "qqmusic":
        song_mid = (req.song_mid or "").strip()
        title = (req.title or "").strip()
        artist = (req.artist or "").strip()
        album = (req.album or "").strip()
        album_mid = (req.album_mid or "").strip()
        duration_ms = req.duration_ms
        quality = (req.quality or "320").strip() or "320"
        cover_url = (req.cover_url or "").strip()

        if not song_mid:
            if not keywords:
                raise HTTPException(status_code=400, detail="song_mid or keywords is required for qqmusic")
            songs = await qqmusic.search_songs_simple(keywords, limit=1, page=1)
            items = _normalize_qqmusic_search_items(songs)
            if not items:
                raise HTTPException(status_code=404, detail="qqmusic song not found")
            first = items[0]
            song_mid = str(first.get("song_mid") or "").strip()
            title = title or str(first.get("title") or song_mid).strip()
            artist = artist or str(first.get("artist") or "").strip()
            album = album or str(first.get("album") or "").strip()
            album_mid = album_mid or str(first.get("album_mid") or "").strip()
            cover_url = cover_url or str(first.get("artwork_url") or "").strip()
            duration_ms = duration_ms if duration_ms is not None else _coerce_positive_int(first.get("duration_ms"))

        if not song_mid:
            raise HTTPException(status_code=400, detail="song_mid is empty")
        if not title:
            title = song_mid
        if not cover_url and album_mid:
            cover_url = qqmusic.get_song_cover_image(album_mid)

        item_id, trial = await _enqueue_qqmusic_song(
            song_mid=song_mid,
            title=title,
            artist=artist,
            play_now=play_now,
            requested_by="external_api",
            quality=quality,
            album_mid=album_mid,
            album=album,
            artwork_url=cover_url,
            duration_ms=duration_ms,
        )
        return {
            "ok": True,
            "source": provider,
            "queue_id": item_id,
            "trial": trial,
            "play_now": play_now,
            "track": {
                "source": provider,
                "track_id": f"qqmusic:{song_mid}",
                "song_mid": song_mid,
                "title": title,
                "artist": artist,
                "album": album,
                "album_mid": album_mid,
                "duration_ms": duration_ms,
                "artwork_url": cover_url,
                "quality": quality,
            },
        }

    raise HTTPException(status_code=400, detail="unsupported source")


@app.get("/external/queue", tags=["External API"])
def external_get_queue(session: Session = Depends(get_session)) -> dict:
    items = get_queue(session=session)
    return {"count": len(items), "items": items}


@app.delete("/external/queue", tags=["External API"])
async def external_clear_queue(session: Session = Depends(get_session)) -> dict:
    return await clear_queue(session=session)


@app.delete("/external/queue/{item_id}", tags=["External API"])
def external_delete_queue_item(item_id: int, session: Session = Depends(get_session)) -> dict:
    return delete_queue_item(item_id=item_id, session=session)


@app.post("/external/queue/{item_id}/play", tags=["External API"])
async def external_play_queue_item(item_id: int, session: Session = Depends(get_session)) -> dict:
    return await play_queue_item(item_id=item_id, session=session)


@app.get("/external/history", tags=["External API"])
def external_history(session: Session = Depends(get_session)) -> dict:
    items = history(session=session)
    return {"count": len(items), "items": items}


@app.post("/external/history/{history_id}/replay", tags=["External API"])
async def external_replay_history(
    history_id: int,
    play_now: bool = True,
    session: Session = Depends(get_session),
) -> dict:
    hist_item = session.get(HistoryItem, history_id)
    if not hist_item:
        raise HTTPException(status_code=404, detail="History item not found")
    return await _replay_history_item(hist_item, play_now=play_now, requested_by="external_history")


# QQ 音乐 API 端点

@app.get("/qqmusic/search")
async def qqmusic_search(keywords: str, search_type: int = 0, limit: int = 50, page: int = 1) -> dict:
    """QQ音乐搜索"""
    try:
        result = await qqmusic.search_with_keyword(keywords, search_type, limit, page)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/search/songs")
async def qqmusic_search_songs(keywords: str, limit: int = 50, page: int = 1) -> dict:
    """QQ音乐搜索歌曲（简化版）"""
    try:
        songs = await qqmusic.search_songs_simple(keywords, limit, page)
        return {"songs": songs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/search/playlists")
async def qqmusic_search_playlists(keywords: str, limit: int = 30, page: int = 1) -> dict:
    """QQ 音乐歌单搜索（返回稳定的前端结构）。"""
    query = (keywords or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="keywords is empty")
    try:
        raw = await qqmusic.search_with_keyword(
            query,
            search_type=3,
            result_num=max(1, min(int(limit), 50)),
            page_num=max(1, int(page)),
        )
        items = _extract_qqmusic_playlist_search_items(raw)
        return {
            "keywords": query,
            "page": max(1, int(page)),
            "limit": max(1, min(int(limit), 50)),
            "has_more": len(items) >= max(1, min(int(limit), 50)),
            "playlists": items,
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/song/{song_mid}/url")
async def qqmusic_song_url(song_mid: str, quality: str = "320") -> dict:
    """获取QQ音乐播放URL"""
    try:
        data = await qqmusic.get_music_url(song_mid, quality)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/song/{song_mid}/lyric")
async def qqmusic_song_lyric(song_mid: str, parse: bool = False) -> dict:
    """获取QQ音乐歌词"""
    try:
        if parse:
            data = await qqmusic.get_song_lyric(song_mid)
            parsed = qqmusic.parse_lyric(data)
            return {"lyric": parsed}
        else:
            lyric = await qqmusic.get_song_lyric_simple(song_mid)
            return {"lyric": lyric}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/playlist/{playlist_id}")
async def qqmusic_playlist_detail(playlist_id: str) -> dict:
    """获取QQ音乐歌单详情"""
    try:
        data = await qqmusic.get_song_list(playlist_id)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/playlist/{playlist_id}/songs")
async def qqmusic_playlist_songs(playlist_id: str) -> dict:
    """获取QQ音乐歌单歌曲列表（简化版）"""
    try:
        songs = await qqmusic.get_song_list_simple(playlist_id)
        return {"songs": songs}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/playlist/{playlist_id}/name")
async def qqmusic_playlist_name(playlist_id: str) -> dict:
    """获取QQ音乐歌单名称"""
    try:
        name = await qqmusic.get_song_list_name_simple(playlist_id)
        return {"name": name}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/album/{album_mid}")
async def qqmusic_album_detail(album_mid: str) -> dict:
    """获取QQ音乐专辑详情"""
    try:
        data = await qqmusic.get_album_song_list(album_mid)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/album/{album_mid}/name")
async def qqmusic_album_name(album_mid: str) -> dict:
    """获取QQ音乐专辑名称"""
    try:
        data = await qqmusic.get_album_name(album_mid)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/singer/{singer_mid}")
async def qqmusic_singer_info(singer_mid: str) -> dict:
    """获取QQ音乐歌手信息"""
    try:
        data = await qqmusic.get_singer_info(singer_mid)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/mv/{vid}")
async def qqmusic_mv_info(vid: str) -> dict:
    """获取QQ音乐MV信息"""
    try:
        data = await qqmusic.get_mv_info(vid)
        return data
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/album/{album_mid}/cover")
async def qqmusic_album_cover(album_mid: str) -> dict:
    """获取QQ音乐专辑封面URL"""
    try:
        cover_url = qqmusic.get_album_cover_image(album_mid)
        return {"cover_url": cover_url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# QQ Music Login endpoints

@app.get("/qqmusic/login/qr/key")
async def qqmusic_qr_key() -> dict:
    """获取QQ音乐二维码登录密钥"""
    try:
        return await qqmusic.get_qr_key()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/login/qr/check")
async def qqmusic_qr_check(qr_key: str, ptqrtoken: str, pt_login_sig: str = "") -> dict:
    """检查QQ音乐二维码登录状态"""
    try:
        # Set the pt_login_sig in the client if provided
        if pt_login_sig:
            qqmusic._pt_login_sig = pt_login_sig
        return await qqmusic.check_qr_status(qr_key, ptqrtoken)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class QQMusicQRCheckRequest(BaseModel):
    qr_key: str
    ptqrtoken: str
    pt_login_sig: str = ""


@app.post("/qqmusic/login/qr/check")
async def qqmusic_qr_check_post(req: QQMusicQRCheckRequest) -> dict:
    return await qqmusic_qr_check(req.qr_key, req.ptqrtoken, req.pt_login_sig)


class QQMusicCookieSetRequest(BaseModel):
    cookie: str


class QQMusicQRConfirmRequest(BaseModel):
    auth_url: str


@app.get("/admin/qqmusic/status")
async def admin_qqmusic_status(request: Request, session: Session = Depends(get_session)) -> dict:
    _require_admin_token(request)
    try:
        _get_admin_qqmusic_cookie(session)
    except HTTPException:
        return {"admin_cookie_set": False}
    return {"admin_cookie_set": True}


@app.post("/admin/qqmusic/cookie")
async def admin_qqmusic_set_cookie(
    req: QQMusicCookieSetRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    _require_admin_token(request)
    c = (req.cookie or "").strip()
    if not c:
        raise HTTPException(status_code=400, detail="cookie is empty")
    if c.lower().startswith("cookie:"):
        c = c.split(":", 1)[1].strip()
    c = c.replace("\r", "").replace("\n", "")
    _set_secret(session, "qqmusic_cookie", c)
    qqmusic.set_cookie(c)
    return {"ok": True, "admin_cookie_set": True, "uin": qqmusic.get_uin()}


@app.delete("/admin/qqmusic/cookie")
async def admin_qqmusic_delete_cookie(request: Request, session: Session = Depends(get_session)) -> dict:
    _require_admin_token(request)
    row = session.get(Secret, "qqmusic_cookie")
    if row:
        session.delete(row)
        session.commit()
    qqmusic.set_cookie("")
    qqmusic._session_cookies = {}
    return {"ok": True, "admin_cookie_set": False}


@app.post("/admin/qqmusic/qr/confirm")
async def admin_qqmusic_qr_confirm(
    req: QQMusicQRConfirmRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> dict:
    """管理员扫码成功后确认登录，获取并保存最终 cookies"""
    _require_admin_token(request)
    try:
        r = await qqmusic.confirm_qr_login(req.auth_url)
    except Exception as exc:
        logger.warning("QQ 音乐扫码：授权回调失败，异常类型 %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail="QQ 音乐扫码授权回调失败，请查看后端日志") from None
    c = (qqmusic.get_cookie() or "").strip()
    if c:
        try:
            _set_secret(session, "qqmusic_cookie", c)
        except Exception as exc:
            logger.warning("QQ 音乐扫码：保存登录信息失败，异常类型 %s", type(exc).__name__)
            raise HTTPException(status_code=500, detail="保存 QQ 音乐登录信息失败，请查看后端日志") from None
        logger.info("QQ 音乐扫码：登录信息已保存到数据库，尚未验证有效性")
    else:
        logger.warning("QQ 音乐扫码：未获得登录信息，未保存")
    return {"ok": True, "admin_cookie_set": bool(c), "uin": qqmusic.get_uin(), "raw": r}


@app.post("/qqmusic/login/cookie")
async def qqmusic_set_cookie(req: QQMusicCookieSetRequest) -> dict:
    """设置QQ音乐Cookie"""
    try:
        qqmusic.set_cookie(req.cookie)
        return {"success": True, "message": "Cookie设置成功", "uin": qqmusic.get_uin()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/login/status")
async def qqmusic_login_status() -> dict:
    """获取QQ音乐登录状态"""
    try:
        if not qqmusic.get_cookie():
            return {"logged_in": False, "message": "未设置Cookie"}
        
        refresh_result = await qqmusic.refresh_login()
        return {
            "logged_in": refresh_result["success"],
            "message": refresh_result["message"],
            "uin": qqmusic.get_uin(),
            "cookie": qqmusic.get_cookie()
        }
    except Exception as e:
        return {"logged_in": False, "message": str(e)}


@app.get("/qqmusic/user/info")
async def qqmusic_user_info() -> dict:
    """获取QQ音乐用户信息"""
    try:
        return await qqmusic.get_user_info()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/qqmusic/user/playlists")
async def qqmusic_user_playlists() -> dict:
    """获取QQ音乐用户歌单"""
    try:
        return await qqmusic.get_user_playlists()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/qqmusic/login/refresh")
async def qqmusic_refresh_login() -> dict:
    """刷新QQ音乐登录状态"""
    try:
        return await qqmusic.refresh_login()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
