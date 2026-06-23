from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

ALBUM_PATTERN = re.compile(r"(?i)\bJM\s*(\d{1,12})\b")
CQ_CODE_PATTERN = re.compile(r"\[CQ:([a-zA-Z0-9_]+)((?:,[^\]]*)?)\]")


class ParseAction(StrEnum):
    IGNORE = "ignore"
    USAGE = "usage"
    OK = "ok"
    ERROR = "error"


@dataclass(frozen=True)
class ParseResult:
    action: ParseAction
    album_id: str | None = None
    error_message: str | None = None


def _decode_cq_value(value: str) -> str:
    return (
        value.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _parse_cq_data(raw_data: str) -> dict[str, str]:
    data: dict[str, str] = {}
    if not raw_data:
        return data
    for item in raw_data.lstrip(",").split(","):
        if not item or "=" not in item:
            continue
        key, value = item.split("=", 1)
        data[key] = _decode_cq_value(value)
    return data


def normalize_message_segments(message_segments: Any) -> list[dict[str, Any]]:
    if isinstance(message_segments, list):
        return [segment for segment in message_segments if isinstance(segment, dict)]
    if not isinstance(message_segments, str):
        return []

    segments: list[dict[str, Any]] = []
    cursor = 0
    for match in CQ_CODE_PATTERN.finditer(message_segments):
        if match.start() > cursor:
            text = _decode_cq_value(message_segments[cursor : match.start()])
            if text:
                segments.append({"type": "text", "data": {"text": text}})
        segment_type = match.group(1)
        segments.append({"type": segment_type, "data": _parse_cq_data(match.group(2))})
        cursor = match.end()

    if cursor < len(message_segments):
        text = _decode_cq_value(message_segments[cursor:])
        if text:
            segments.append({"type": "text", "data": {"text": text}})
    return segments


def has_at_bot(message_segments: Any, bot_qq_id: str) -> bool:
    for segment in normalize_message_segments(message_segments):
        if not isinstance(segment, dict):
            continue
        if segment.get("type") != "at":
            continue
        data = segment.get("data") or {}
        if str(data.get("qq")) == str(bot_qq_id):
            return True
    return False


def text_from_segments(message_segments: Any) -> str:
    parts: list[str] = []
    for segment in normalize_message_segments(message_segments):
        if segment.get("type") != "text":
            continue
        data = segment.get("data") or {}
        text = data.get("text")
        if isinstance(text, str):
            parts.append(text)
    return " ".join(parts)


def extract_album_id(message_segments: Any) -> tuple[str | None, str | None]:
    text = text_from_segments(message_segments)
    matches = [match.group(1) for match in ALBUM_PATTERN.finditer(text)]
    if not matches:
        return None, None
    if len(matches) > 1:
        return None, "一条消息只能包含一个 JM 编号"
    return matches[0], None


def parse_group_message(event: dict[str, Any], bot_qq_id: str) -> ParseResult:
    if event.get("message_type") != "group":
        return ParseResult(ParseAction.IGNORE)

    if str(event.get("user_id")) == str(bot_qq_id):
        return ParseResult(ParseAction.IGNORE)

    message_segments = event.get("message")
    if not has_at_bot(message_segments, bot_qq_id):
        return ParseResult(ParseAction.IGNORE)

    album_id, error = extract_album_id(message_segments)
    if error:
        return ParseResult(ParseAction.ERROR, error_message=error)
    if album_id is None:
        return ParseResult(ParseAction.USAGE)
    return ParseResult(ParseAction.OK, album_id=album_id)
