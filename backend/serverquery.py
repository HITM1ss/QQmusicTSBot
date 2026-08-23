from __future__ import annotations

import asyncio
from dataclasses import dataclass


_COMMAND_TIMEOUT_S = 5.0


class ServerQueryError(RuntimeError):
    """A safe, user-facing TeamSpeak ServerQuery failure."""


@dataclass(frozen=True)
class ServerQueryConfig:
    host: str
    port: int
    username: str
    password: str
    virtual_server_sid: str = ""
    virtual_server_port: int = 9987


def escape_value(value: object) -> str:
    """Escape one TeamSpeak ServerQuery argument value."""
    replacements = {
        "\\": "\\\\",
        " ": "\\s",
        "|": "\\p",
        "/": "\\/",
        "\n": "\\n",
        "\r": "\\r",
        "\t": "\\t",
    }
    return "".join(replacements.get(character, character) for character in str(value or ""))


def unescape_value(value: str) -> str:
    """Decode TeamSpeak ServerQuery escape sequences used in log records."""
    replacements = {
        "\\": "\\",
        "s": " ",
        "p": "|",
        "/": "/",
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
    }
    decoded: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character == "\\" and index + 1 < len(value):
            escaped = value[index + 1]
            decoded.append(replacements.get(escaped, escaped))
            index += 2
            continue
        decoded.append(character)
        index += 1
    return "".join(decoded)


def parse_log_lines(response_lines: list[str]) -> list[str]:
    """Extract chronological server log entries from ``logview`` responses."""
    entries: list[str] = []
    for response_line in response_lines:
        for record in response_line.split("|"):
            for token in record.split():
                if token.startswith("l="):
                    entries.append(unescape_value(token[2:]))
                    break
    # ``logview reverse=1`` returns newest first. The UI reads more naturally
    # with the latest record at the bottom, like ``tail``.
    entries.reverse()
    return entries


async def _consume_greeting(reader: asyncio.StreamReader) -> None:
    """Discard the short ServerQuery greeting without delaying real requests."""
    for _ in range(3):
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=0.2)
        except asyncio.TimeoutError:
            return
        if not raw:
            return


def _parse_error(line: str) -> tuple[int, str]:
    error_id = -1
    message = ""
    for token in line.split():
        key, separator, value = token.partition("=")
        if not separator:
            continue
        if key == "id":
            try:
                error_id = int(value)
            except ValueError:
                pass
        elif key == "msg":
            message = unescape_value(value)
    return error_id, message


async def _execute(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    command: str,
) -> list[str]:
    writer.write((command + "\n").encode("utf-8"))
    try:
        await asyncio.wait_for(writer.drain(), timeout=_COMMAND_TIMEOUT_S)
    except (ConnectionError, asyncio.TimeoutError) as exc:
        raise ServerQueryError("写入 ServerQuery 请求失败") from exc

    response_lines: list[str] = []
    while True:
        try:
            raw = await asyncio.wait_for(reader.readline(), timeout=_COMMAND_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise ServerQueryError("等待 ServerQuery 响应超时") from exc
        if not raw:
            raise ServerQueryError("ServerQuery 提前关闭了连接")
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if not line:
            continue
        if line.startswith("error"):
            error_id, message = _parse_error(line)
            if error_id == 0:
                return response_lines
            detail = message or "未知错误"
            raise ServerQueryError(f"ServerQuery 错误 {error_id}: {detail}")
        response_lines.append(line)


async def query_server_logs(config: ServerQueryConfig, *, lines: int) -> list[str]:
    """Return recent TeamSpeak server logs using the read-only ``logview`` command."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(config.host, config.port),
            timeout=_COMMAND_TIMEOUT_S,
        )
    except (OSError, asyncio.TimeoutError) as exc:
        raise ServerQueryError("无法连接到 ServerQuery") from exc

    try:
        await _consume_greeting(reader)
        await _execute(
            reader,
            writer,
            "login client_login_name={} client_login_password={}".format(
                escape_value(config.username),
                escape_value(config.password),
            ),
        )
        if config.virtual_server_sid:
            await _execute(reader, writer, f"use sid={escape_value(config.virtual_server_sid)}")
        else:
            await _execute(reader, writer, f"use port={int(config.virtual_server_port)}")
        response_lines = await _execute(
            reader,
            writer,
            f"logview lines={int(lines)} begin_pos=0 reverse=1 instance=0",
        )
        return parse_log_lines(response_lines)
    finally:
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
        except (ConnectionError, asyncio.TimeoutError):
            pass
