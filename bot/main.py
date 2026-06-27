from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from .backend_client import BackendClient, BackendError, DuplicateJobError, JobLimitError
from .message_parser import (
    ParseAction,
    normalize_message_segments,
    parse_group_message,
    text_from_segments,
)
from .napcat_client import NapCatAPIError, NapCatClient
from .lang import text as lang_text, words as lang_words

logger = logging.getLogger(__name__)

ILLEGAL_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
DEFAULT_CONFIRM_WORDS = {"下载", "确认", "同意", "是", "要", "y", "yes", "ok"}
DEFAULT_CANCEL_WORDS = {"取消", "取消下载", "取消任务", "不要", "否", "不下", "n", "no"}
DEFAULT_ACTIVE_CANCEL_WORDS = DEFAULT_CANCEL_WORDS | {"停止下载", "停止任务"}
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
MAX_FILENAME_BYTES = 180
MAX_UPLOAD_FILENAME_BYTES = 96
MAX_UPLOAD_FALLBACK_DEPTH = 1
DEFAULT_UPLOAD_RETRIES = 5
DEFAULT_SEARCH_RESULT_LIMIT = 5
DEFAULT_USER_COMMAND_COOLDOWN_SECONDS = 10
COVER_SEND_RETRIES = 3
COVER_DOWNLOAD_TIMEOUT_SECONDS = 20
MAX_COVER_IMAGE_BYTES = 8 * 1024 * 1024
GROUP_ADMIN_ROLES = {"owner", "admin"}


class UploadPreparationError(Exception):
    pass


class UploadCancelledError(Exception):
    pass


@dataclass(frozen=True)
class AdminCommand:
    name: str
    target: str | None = None


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Invalid integer for %s; using %s.", name, default)
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_id_set(name: str) -> set[str]:
    value = os.getenv(name, "")
    ids = {piece.strip() for piece in value.split(",") if piece.strip()}
    return {item for item in ids if item.isdigit()}


@dataclass(frozen=True)
class BotSettings:
    bot_qq_id: str
    napcat_ws_url: str
    napcat_http_url: str
    napcat_access_token: str | None
    backend_url: str
    backend_api_token: str | None
    data_dir: Path
    job_timeout_seconds: int
    poll_interval_seconds: float = 5.0
    progress_notify_seconds: int = 300
    confirm_timeout_seconds: int = 600
    large_album_warning_pages: int = 100
    napcat_http_timeout_seconds: int = 60
    napcat_upload_timeout_seconds: int = 900
    max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES
    max_upload_filename_bytes: int = MAX_UPLOAD_FILENAME_BYTES
    upload_retries: int = DEFAULT_UPLOAD_RETRIES
    enable_search: bool = False
    search_result_limit: int = DEFAULT_SEARCH_RESULT_LIMIT
    search_confirm_timeout_seconds: int = 600
    user_command_cooldown_seconds: int = DEFAULT_USER_COMMAND_COOLDOWN_SECONDS
    manager_qq_ids: set[str] = field(default_factory=set)

    @classmethod
    def from_env(cls) -> "BotSettings":
        load_dotenv()
        bot_qq_id = os.getenv("BOT_QQ_ID")
        if not bot_qq_id:
            raise RuntimeError("BOT_QQ_ID is required")
        return cls(
            bot_qq_id=bot_qq_id,
            napcat_ws_url=os.getenv("NAPCAT_WS_URL", "ws://127.0.0.1:3001"),
            napcat_http_url=os.getenv("NAPCAT_HTTP_URL", "http://127.0.0.1:3000"),
            napcat_access_token=os.getenv("NAPCAT_ACCESS_TOKEN") or None,
            backend_url=os.getenv("BACKEND_URL", "http://127.0.0.1:8000"),
            backend_api_token=os.getenv("BACKEND_API_TOKEN") or None,
            data_dir=Path(os.getenv("DATA_DIR", "./data")),
            job_timeout_seconds=max(1, _env_int("JOB_TIMEOUT_SECONDS", 1800)),
            poll_interval_seconds=max(1, _env_int("JOB_POLL_INTERVAL_SECONDS", 5)),
            progress_notify_seconds=max(0, _env_int("JOB_PROGRESS_NOTIFY_SECONDS", 300)),
            confirm_timeout_seconds=max(30, _env_int("JOB_CONFIRM_TIMEOUT_SECONDS", 600)),
            large_album_warning_pages=max(0, _env_int("LARGE_ALBUM_WARNING_PAGES", 100)),
            napcat_http_timeout_seconds=max(1, _env_int("NAPCAT_HTTP_TIMEOUT_SECONDS", 60)),
            napcat_upload_timeout_seconds=max(60, _env_int("NAPCAT_UPLOAD_TIMEOUT_SECONDS", 900)),
            max_upload_bytes=max(0, _env_int("NAPCAT_MAX_UPLOAD_BYTES", DEFAULT_MAX_UPLOAD_BYTES)),
            max_upload_filename_bytes=max(
                32,
                _env_int("NAPCAT_MAX_UPLOAD_FILENAME_BYTES", MAX_UPLOAD_FILENAME_BYTES),
            ),
            upload_retries=max(1, _env_int("NAPCAT_UPLOAD_RETRIES", DEFAULT_UPLOAD_RETRIES)),
            enable_search=_env_bool("ENABLE_SEARCH", False),
            search_result_limit=max(1, min(10, _env_int("SEARCH_RESULT_LIMIT", DEFAULT_SEARCH_RESULT_LIMIT))),
            search_confirm_timeout_seconds=max(30, _env_int("SEARCH_CONFIRM_TIMEOUT_SECONDS", 600)),
            user_command_cooldown_seconds=max(
                0,
                _env_int("USER_COMMAND_COOLDOWN_SECONDS", DEFAULT_USER_COMMAND_COOLDOWN_SECONDS),
            ),
            manager_qq_ids=_env_id_set("BOT_MANAGER_QQ_IDS"),
        )


@dataclass(frozen=True)
class PendingDownload:
    album_id: str
    title: str
    estimated_text: str
    page_count: int | None
    expires_at: float
    large_warning_sent: bool = False


@dataclass(frozen=True)
class PendingSearch:
    query: str
    results: list[dict[str, Any]]
    expires_at: float


@dataclass(frozen=True)
class UploadingJob:
    job_id: str
    album_id: str
    group_id: str
    user_id: str
    started_at: float


@dataclass
class BotState:
    pending_downloads: dict[tuple[str, str], PendingDownload] = field(default_factory=dict)
    pending_searches: dict[tuple[str, str], PendingSearch] = field(default_factory=dict)
    uploading_jobs: dict[str, UploadingJob] = field(default_factory=dict)
    cancelled_uploads: set[str] = field(default_factory=set)
    command_cooldowns: dict[tuple[str, str], float] = field(default_factory=dict)

    def cleanup(self, now: float) -> None:
        expired_downloads = [
            key
            for key, pending in self.pending_downloads.items()
            if pending.expires_at <= now
        ]
        for key in expired_downloads:
            self.pending_downloads.pop(key, None)
        expired_searches = [
            key
            for key, pending in self.pending_searches.items()
            if pending.expires_at <= now
        ]
        for key in expired_searches:
            self.pending_searches.pop(key, None)


def _safe_filename(name: str, fallback: str, max_bytes: int = MAX_FILENAME_BYTES) -> str:
    cleaned = ILLEGAL_FILENAME_CHARS_RE.sub("_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    cleaned = cleaned or fallback
    if len(cleaned.encode("utf-8")) <= max_bytes:
        return cleaned

    suffix = Path(cleaned).suffix
    stem = Path(cleaned).stem if suffix else cleaned
    suffix_bytes = len(suffix.encode("utf-8"))
    stem_budget = max(1, max_bytes - suffix_bytes)
    stem = stem.encode("utf-8")[:stem_budget].decode("utf-8", errors="ignore").strip(" .")
    if stem:
        return f"{stem}{suffix}"
    return fallback


def _confirm_words() -> set[str]:
    return lang_words("confirm_words", DEFAULT_CONFIRM_WORDS)


def _cancel_words() -> set[str]:
    return lang_words("cancel_words", DEFAULT_CANCEL_WORDS)


def _active_cancel_words() -> set[str]:
    return lang_words("active_cancel_words", DEFAULT_ACTIVE_CANCEL_WORDS)


def _parse_admin_command(event: dict[str, Any], bot_qq_id: str) -> AdminCommand | None:
    if not _has_at_bot(event, bot_qq_id):
        return None
    text = text_from_segments(event.get("message")).strip()
    normalized = re.sub(r"\s+", " ", text).strip()
    if normalized in {"状态", "status"}:
        return AdminCommand("status")
    if normalized in {"队列", "queue"}:
        return AdminCommand("queue")
    if normalized in {"清理缓存", "清除缓存", "cleanup"}:
        return AdminCommand("cleanup")

    cancel_match = re.match(r"^(?:取消|cancel)\s+(.+)$", normalized, flags=re.I)
    if cancel_match:
        target = cancel_match.group(1).strip()
        if target and target not in _active_cancel_words():
            return AdminCommand("cancel", target=target)
    return None


def _has_at_bot(event: dict[str, Any], bot_qq_id: str) -> bool:
    for segment in normalize_message_segments(event.get("message")):
        if segment.get("type") != "at":
            continue
        data = segment.get("data") or {}
        if str(data.get("qq")) == str(bot_qq_id):
            return True
    return False


def _sender_role(event: dict[str, Any]) -> str:
    sender = event.get("sender")
    if not isinstance(sender, dict):
        return ""
    return str(sender.get("role") or "").lower()


def _is_manager(user_id: str, settings: BotSettings) -> bool:
    return str(user_id) in settings.manager_qq_ids


def _is_group_admin(event: dict[str, Any]) -> bool:
    return _sender_role(event) in GROUP_ADMIN_ROLES


def _can_run_admin_command(event: dict[str, Any], user_id: str, settings: BotSettings) -> bool:
    return _is_manager(user_id, settings) or _is_group_admin(event)


async def handle_group_message(
    event: dict[str, Any],
    settings: BotSettings,
    state: BotState,
    napcat: NapCatClient,
    backend: BackendClient,
    spawn_task: Callable[[Awaitable[None]], None],
) -> None:
    if event.get("message_type") != "group":
        return
    if str(event.get("user_id")) == str(settings.bot_qq_id):
        return

    group_id = str(event.get("group_id") or "")
    user_id = str(event.get("user_id") or "")
    if not group_id or not user_id:
        return

    segments = normalize_message_segments(event.get("message"))
    segment_types = [str(segment.get("type")) for segment in segments[:10]]
    at_targets = [
        str((segment.get("data") or {}).get("qq"))
        for segment in segments
        if segment.get("type") == "at"
    ]
    message_text = text_from_segments(event.get("message"))[:120]
    log_message = logger.info if at_targets or "JM" in message_text.upper() else logger.debug
    log_message(
        "Received group message group_id=%s user_id=%s segment_types=%s at_targets=%s bot_qq_id=%s text=%r",
        group_id,
        user_id,
        segment_types,
        at_targets,
        settings.bot_qq_id,
        message_text,
    )

    now = asyncio.get_running_loop().time()
    state.cleanup(now)
    if await _handle_admin_command(event, group_id, user_id, settings, state, napcat, backend):
        return
    if await _handle_pending_confirmation(event, group_id, user_id, settings, state, napcat, backend, spawn_task):
        return
    if await _handle_pending_search_selection(event, group_id, user_id, settings, state, napcat, backend):
        return
    if await _handle_active_cancel(event, group_id, user_id, napcat, backend):
        return

    parse_result = parse_group_message(event, settings.bot_qq_id)
    if parse_result.action == ParseAction.IGNORE:
        if at_targets:
            logger.info(
                "Ignored group message because it did not match this bot or command: at_targets=%s bot_qq_id=%s text=%r",
                at_targets,
                settings.bot_qq_id,
                message_text,
            )
        return

    logger.info(
        "Parsed group command action=%s album_id=%s search_query=%s group_id=%s user_id=%s",
        parse_result.action,
        parse_result.album_id,
        parse_result.search_query,
        group_id,
        user_id,
    )

    if parse_result.action == ParseAction.USAGE:
        await _safe_send(napcat, group_id, lang_text("usage"))
        return

    if parse_result.action == ParseAction.ERROR:
        await _safe_send(napcat, group_id, lang_text(parse_result.error_key or "usage"))
        return

    if parse_result.action in {ParseAction.OK, ParseAction.SEARCH}:
        remaining = _command_cooldown_remaining(group_id, user_id, settings, state, now)
        if remaining > 0:
            await _safe_send(napcat, group_id, lang_text("command_cooldown", seconds=math.ceil(remaining)))
            return
        _mark_command_cooldown(group_id, user_id, settings, state, now)

    if parse_result.action == ParseAction.SEARCH:
        await _handle_search_command(
            parse_result.search_query or "",
            group_id,
            user_id,
            settings,
            state,
            napcat,
            backend,
        )
        return

    album_id = parse_result.album_id
    if album_id is None:
        await _safe_send(napcat, group_id, lang_text("usage"))
        return

    await _safe_send(napcat, group_id, lang_text("received_fetching", album_id=album_id))

    try:
        active = await backend.get_active_job(group_id, user_id)
    except BackendError as exc:
        logger.exception("Could not query active job for group=%s user=%s.", group_id, user_id)
        await _safe_send(napcat, group_id, lang_text("backend_unavailable", error_code=exc.error_code))
        return

    if active is not None:
        await _safe_send(
            napcat,
            group_id,
            lang_text("active_job_exists", album_id=active.get("album_id")),
        )
        return

    state.pending_searches.pop((group_id, user_id), None)
    await _send_album_preview(album_id, group_id, user_id, settings, state, napcat, backend)


async def _handle_admin_command(
    event: dict[str, Any],
    group_id: str,
    user_id: str,
    settings: BotSettings,
    state: BotState,
    napcat: NapCatClient,
    backend: BackendClient,
) -> bool:
    command = _parse_admin_command(event, settings.bot_qq_id)
    if command is None:
        return False

    if command.name == "cleanup":
        if not _is_manager(user_id, settings):
            await _safe_send(napcat, group_id, lang_text("admin_manager_required"))
            return True
    elif not _can_run_admin_command(event, user_id, settings):
        await _safe_send(napcat, group_id, lang_text("admin_permission_denied"))
        return True

    if command.name == "status":
        await _send_admin_status(group_id, state, napcat, backend)
    elif command.name == "queue":
        await _send_admin_queue(group_id, state, napcat, backend)
    elif command.name == "cleanup":
        await _run_admin_cleanup(group_id, state, napcat, backend)
    elif command.name == "cancel":
        await _run_admin_cancel(group_id, command.target or "", state, napcat, backend)
    return True


async def _send_admin_status(
    group_id: str,
    state: BotState,
    napcat: NapCatClient,
    backend: BackendClient,
) -> None:
    try:
        payload = await backend.get_admin_status()
    except BackendError as exc:
        logger.exception("Could not fetch admin status.")
        await _safe_send(napcat, group_id, lang_text("admin_status_failed", error_code=exc.error_code))
        return

    await _safe_send(napcat, group_id, _format_admin_status(payload, len(state.uploading_jobs)))


async def _send_admin_queue(
    group_id: str,
    state: BotState,
    napcat: NapCatClient,
    backend: BackendClient,
) -> None:
    try:
        payload = await backend.get_admin_queue()
    except BackendError as exc:
        logger.exception("Could not fetch admin queue.")
        await _safe_send(napcat, group_id, lang_text("admin_queue_failed", error_code=exc.error_code))
        return

    jobs = payload.get("jobs")
    safe_jobs = [job for job in jobs if isinstance(job, dict)] if isinstance(jobs, list) else []
    await _safe_send(napcat, group_id, _format_admin_queue(_merge_uploading_jobs(safe_jobs, state)))


async def _run_admin_cleanup(
    group_id: str,
    state: BotState,
    napcat: NapCatClient,
    backend: BackendClient,
) -> None:
    if state.uploading_jobs:
        await _safe_send(napcat, group_id, lang_text("admin_cleanup_busy", count=len(state.uploading_jobs)))
        return

    try:
        payload = await backend.cleanup_cache()
    except BackendError as exc:
        logger.exception("Could not cleanup cache.")
        await _safe_send(napcat, group_id, lang_text("admin_cleanup_failed", error=exc, error_code=exc.error_code))
        return

    stats = payload.get("stats") if isinstance(payload, dict) else {}
    stats = stats if isinstance(stats, dict) else {}
    await _safe_send(
        napcat,
        group_id,
        lang_text(
            "admin_cleanup_done",
            freed=_format_bytes(int(payload.get("freed_bytes") or 0)),
            job_dirs=int(stats.get("job_dirs") or 0),
            bot_downloads=int(stats.get("bot_downloads") or 0),
            previews=int(stats.get("previews") or 0),
        ),
    )


async def _run_admin_cancel(
    group_id: str,
    target: str,
    state: BotState,
    napcat: NapCatClient,
    backend: BackendClient,
) -> None:
    target = _normalize_cancel_target(target)
    if not target:
        await _safe_send(napcat, group_id, lang_text("admin_cancel_usage"))
        return

    uploading_job = _find_uploading_job(target, state)
    if uploading_job is not None:
        state.cancelled_uploads.add(uploading_job.job_id)
        await _safe_send(
            napcat,
            group_id,
            lang_text("admin_cancel_uploading", job_id=_short_job_id(uploading_job.job_id), album_id=uploading_job.album_id),
        )
        return

    try:
        payload = await backend.admin_cancel_job(target)
    except BackendError as exc:
        logger.exception("Could not cancel admin target %s.", target)
        await _safe_send(napcat, group_id, lang_text("admin_cancel_failed", target=target, error=exc, error_code=exc.error_code))
        return

    job = payload.get("job") if isinstance(payload, dict) else None
    if not isinstance(job, dict):
        await _safe_send(napcat, group_id, lang_text("admin_cancel_failed", target=target, error="返回结果无效", error_code="BAD_RESPONSE"))
        return

    await _safe_send(
        napcat,
        group_id,
        lang_text(
            "admin_cancel_done",
            job_id=_short_job_id(str(job.get("job_id") or target)),
            album_id=job.get("album_id"),
            status=_status_label(str(job.get("status") or "")),
        ),
    )


def _command_cooldown_remaining(
    group_id: str,
    user_id: str,
    settings: BotSettings,
    state: BotState,
    now: float,
) -> float:
    cooldown = settings.user_command_cooldown_seconds
    if cooldown <= 0:
        return 0.0
    key = (group_id, user_id)
    last_at = state.command_cooldowns.get(key)
    if last_at is None:
        return 0.0
    return max(0.0, cooldown - (now - last_at))


def _mark_command_cooldown(
    group_id: str,
    user_id: str,
    settings: BotSettings,
    state: BotState,
    now: float,
) -> None:
    if settings.user_command_cooldown_seconds <= 0:
        return
    state.command_cooldowns[(group_id, user_id)] = now


async def _handle_pending_confirmation(
    event: dict[str, Any],
    group_id: str,
    user_id: str,
    settings: BotSettings,
    state: BotState,
    napcat: NapCatClient,
    backend: BackendClient,
    spawn_task: Callable[[Awaitable[None]], None],
) -> bool:
    key = (group_id, user_id)
    pending = state.pending_downloads.get(key)
    if pending is None:
        return False

    text = text_from_segments(event.get("message")).strip().lower()
    if not text:
        return False

    if text in _cancel_words():
        state.pending_downloads.pop(key, None)
        await _safe_send(napcat, group_id, lang_text("cancelled_pending", album_id=pending.album_id))
        return True

    if text not in _confirm_words():
        return False

    if _needs_large_album_confirmation(pending.page_count, settings) and not pending.large_warning_sent:
        state.pending_downloads[key] = replace(pending, large_warning_sent=True)
        await _safe_send(
            napcat,
            group_id,
            lang_text(
                "large_album_warning",
                album_id=pending.album_id,
                page_count=pending.page_count,
                limit=settings.large_album_warning_pages,
            ),
        )
        return True

    state.pending_downloads.pop(key, None)
    await _create_job_and_monitor(
        pending.album_id,
        group_id,
        user_id,
        settings,
        napcat,
        backend,
        spawn_task,
        state=state,
        page_count=pending.page_count,
        extra_message=lang_text("estimated_time_line", estimated_text=pending.estimated_text),
    )
    return True


async def _handle_pending_search_selection(
    event: dict[str, Any],
    group_id: str,
    user_id: str,
    settings: BotSettings,
    state: BotState,
    napcat: NapCatClient,
    backend: BackendClient,
) -> bool:
    key = (group_id, user_id)
    pending = state.pending_searches.get(key)
    if pending is None:
        return False

    text = text_from_segments(event.get("message")).strip().lower()
    if not text:
        return False

    if text in _cancel_words():
        state.pending_searches.pop(key, None)
        await _safe_send(napcat, group_id, lang_text("search_cancelled"))
        return True

    if not text.isdigit():
        return False

    index = int(text)
    if index < 1 or index > len(pending.results):
        await _safe_send(napcat, group_id, lang_text("search_invalid_choice", count=len(pending.results)))
        return True

    selected = pending.results[index - 1]
    album_id = str(selected.get("album_id") or "")
    if not album_id.isdigit():
        state.pending_searches.pop(key, None)
        await _safe_send(napcat, group_id, lang_text("search_failed", error="搜索结果无效", error_code="SEARCH_BAD_RESULT"))
        return True

    try:
        active = await backend.get_active_job(group_id, user_id)
    except BackendError as exc:
        logger.exception("Could not query active job for group=%s user=%s.", group_id, user_id)
        await _safe_send(napcat, group_id, lang_text("backend_unavailable", error_code=exc.error_code))
        return True

    if active is not None:
        state.pending_searches.pop(key, None)
        await _safe_send(
            napcat,
            group_id,
            lang_text("active_job_exists", album_id=active.get("album_id")),
        )
        return True

    state.pending_searches.pop(key, None)
    await _safe_send(napcat, group_id, lang_text("search_selected", album_id=album_id))
    await _send_album_preview(album_id, group_id, user_id, settings, state, napcat, backend)
    return True


async def _handle_search_command(
    query: str,
    group_id: str,
    user_id: str,
    settings: BotSettings,
    state: BotState,
    napcat: NapCatClient,
    backend: BackendClient,
) -> None:
    if not settings.enable_search:
        await _safe_send(napcat, group_id, lang_text("search_disabled"))
        return

    query = re.sub(r"\s+", " ", query).strip()
    if not query:
        await _safe_send(napcat, group_id, lang_text("search_usage"))
        return

    await _safe_send(napcat, group_id, lang_text("searching", query=query))
    try:
        payload = await backend.search_albums(query, page=1, limit=settings.search_result_limit)
    except BackendError as exc:
        logger.exception("Could not search albums for group=%s user=%s.", group_id, user_id)
        await _safe_send(napcat, group_id, lang_text("search_failed", error=exc, error_code=exc.error_code))
        return

    results = payload.get("results")
    if not isinstance(results, list) or not results:
        state.pending_searches.pop((group_id, user_id), None)
        await _safe_send(napcat, group_id, lang_text("search_empty", query=query))
        return

    safe_results = [
        result
        for result in results[: settings.search_result_limit]
        if isinstance(result, dict) and str(result.get("album_id") or "").isdigit()
    ]
    if not safe_results:
        state.pending_searches.pop((group_id, user_id), None)
        await _safe_send(napcat, group_id, lang_text("search_empty", query=query))
        return

    state.pending_downloads.pop((group_id, user_id), None)
    state.pending_searches[(group_id, user_id)] = PendingSearch(
        query=query,
        results=safe_results,
        expires_at=asyncio.get_running_loop().time() + settings.search_confirm_timeout_seconds,
    )
    await _safe_send(napcat, group_id, _format_search_results(query, safe_results))


async def _handle_active_cancel(
    event: dict[str, Any],
    group_id: str,
    user_id: str,
    napcat: NapCatClient,
    backend: BackendClient,
) -> bool:
    text = text_from_segments(event.get("message")).strip().lower()
    if text not in _active_cancel_words():
        return False

    try:
        cancelled = await backend.cancel_active_job(group_id, user_id)
    except BackendError as exc:
        logger.exception("Could not cancel active job for group=%s user=%s.", group_id, user_id)
        await _safe_send(napcat, group_id, lang_text("cancel_failed", error_code=exc.error_code))
        return True

    if cancelled is None:
        await _safe_send(napcat, group_id, lang_text("no_active_job"))
        return True

    await _safe_send(napcat, group_id, lang_text("cancelled_active", album_id=cancelled.get("album_id")))
    return True


async def _send_album_preview(
    album_id: str,
    group_id: str,
    user_id: str,
    settings: BotSettings,
    state: BotState,
    napcat: NapCatClient,
    backend: BackendClient,
) -> None:
    try:
        preview = await backend.get_album_preview(album_id)
    except BackendError as exc:
        logger.exception("Could not fetch album preview.")
        await _safe_send(
            napcat,
            group_id,
            lang_text("preview_failed", album_id=album_id, error=exc, error_code=exc.error_code),
        )
        return

    title = str(preview.get("title") or f"JM{album_id}")
    estimated_text = str(preview.get("estimated_text") or lang_text("estimated_unknown"))
    cover_url = preview.get("cover_url")
    page_count = preview.get("page_count")
    page_count_is_estimated = bool(preview.get("page_count_is_estimated"))

    if isinstance(cover_url, str) and cover_url:
        await _send_album_cover(album_id, group_id, cover_url, settings, napcat)

    if isinstance(page_count, int) and page_count > 0:
        page_text = lang_text(
            "page_count_estimated" if page_count_is_estimated else "page_count_exact",
            page_count=page_count,
        )
    else:
        page_text = lang_text("page_count_unknown")
    extra_warning = ""
    if _needs_large_album_confirmation(page_count, settings):
        extra_warning = lang_text("large_album_hint", limit=settings.large_album_warning_pages)

    await _safe_send(
        napcat,
        group_id,
        lang_text(
            "album_preview",
            album_id=album_id,
            title=title,
            page_text=page_text,
            estimated_text=estimated_text,
            extra_warning=extra_warning,
        ),
    )
    state.pending_downloads[(group_id, user_id)] = PendingDownload(
        album_id=album_id,
        title=title,
        estimated_text=estimated_text,
        page_count=page_count if isinstance(page_count, int) and page_count > 0 else None,
        expires_at=asyncio.get_running_loop().time() + settings.confirm_timeout_seconds,
    )


def _needs_large_album_confirmation(page_count: object, settings: BotSettings) -> bool:
    return (
        settings.large_album_warning_pages > 0
        and isinstance(page_count, int)
        and page_count > settings.large_album_warning_pages
    )


def _format_search_results(query: str, results: list[dict[str, Any]]) -> str:
    lines = [lang_text("search_results_header", query=query)]
    for index, result in enumerate(results, start=1):
        album_id = str(result.get("album_id") or "")
        title = _truncate_display_text(str(result.get("title") or f"JM{album_id}"), 46)
        lines.append(lang_text("search_result_line", index=index, album_id=album_id, title=title))
    lines.append(lang_text("search_results_footer", count=len(results)))
    return "\n".join(lines)


def _format_admin_status(payload: dict[str, Any], uploading_count: int) -> str:
    memory = payload.get("memory") if isinstance(payload.get("memory"), dict) else None
    disk = payload.get("disk") if isinstance(payload.get("disk"), dict) else {}
    cache = payload.get("cache") if isinstance(payload.get("cache"), dict) else {}
    network = payload.get("network") if isinstance(payload.get("network"), dict) else {}
    jobs = payload.get("jobs") if isinstance(payload.get("jobs"), dict) else {}

    if memory:
        memory_text = f"{_format_bytes(int(memory.get('used') or 0))} / {_format_bytes(int(memory.get('total') or 0))}"
    else:
        memory_text = "未知"

    cpu = payload.get("cpu_percent")
    cpu_text = f"{float(cpu):.1f}%" if isinstance(cpu, (int, float)) else "未知"

    lines = [
        "服务器状态",
        f"CPU：{cpu_text}",
        f"内存：{memory_text}",
        f"磁盘：{_format_bytes(int(disk.get('used') or 0))} / {_format_bytes(int(disk.get('total') or 0))}，剩余 {_format_bytes(int(disk.get('free') or 0))}",
        f"缓存：data {_format_bytes(int(cache.get('data') or 0))}，jobs {_format_bytes(int(cache.get('jobs') or 0))}，bot {_format_bytes(int(cache.get('bot_downloads') or 0))}",
        f"网络：上行 {_format_rate(network.get('tx_bytes_per_second'))}，下行 {_format_rate(network.get('rx_bytes_per_second'))}",
        f"队列：下载中 {int(jobs.get('downloading') or 0)}，排队 {int(jobs.get('queued') or 0)}，转换中 {int(jobs.get('converting') or 0)}，上传中 {uploading_count}",
    ]
    return "\n".join(lines)


def _format_admin_queue(jobs: list[dict[str, Any]]) -> str:
    if not jobs:
        return lang_text("admin_queue_empty")

    lines = ["当前队列"]
    for index, job in enumerate(jobs[:20], start=1):
        job_id = _short_job_id(str(job.get("job_id") or ""))
        album_id = str(job.get("album_id") or "?")
        group_id = str(job.get("group_id") or "?")
        status_value = str(job.get("status") or "")
        progress = _job_progress_text(job)
        lines.append(
            lang_text(
                "admin_queue_line",
                index=index,
                job_id=job_id,
                album_id=album_id,
                status=progress or _status_label(status_value),
                group_id=group_id,
            )
        )
    return "\n".join(lines)


def _merge_uploading_jobs(jobs: list[dict[str, Any]], state: BotState) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {str(job.get("job_id")): dict(job) for job in jobs if job.get("job_id")}
    for uploading in state.uploading_jobs.values():
        merged[uploading.job_id] = {
            "job_id": uploading.job_id,
            "album_id": uploading.album_id,
            "group_id": uploading.group_id,
            "user_id": uploading.user_id,
            "status": "uploading",
            "downloaded_files": 0,
            "total_files": 0,
            "progress_message": "上传中",
        }
    return list(merged.values())


def _job_progress_text(job: dict[str, Any]) -> str:
    status_value = str(job.get("status") or "")
    if status_value == "failed":
        error_code = job.get("error_code")
        return f"错误：{error_code or 'UNKNOWN'}"
    if status_value == "uploading":
        return "上传中"

    total_files = int(job.get("total_files") or 0)
    downloaded_files = int(job.get("downloaded_files") or 0)
    label = _status_label(status_value)
    if total_files > 0 and status_value in {"downloading", "completed"}:
        ratio = min(100.0, max(0.0, downloaded_files * 100 / total_files))
        if status_value == "completed":
            ratio = 100.0
        return f"{label}（{ratio:.0f}%）"
    return label


def _status_label(status_value: str) -> str:
    return {
        "queued": "排队中",
        "downloading": "下载中",
        "converting": "转换中",
        "completed": "已完成",
        "failed": "错误",
        "uploading": "上传中",
    }.get(status_value, status_value or "未知")


def _short_job_id(job_id: str) -> str:
    return job_id.split("-", 1)[0] if job_id else "?"


def _normalize_cancel_target(target: str) -> str:
    target = target.strip()
    match = re.search(r"(?i)\bJM\s*(\d{1,12})\b", target)
    if match:
        return match.group(1)
    return target


def _find_uploading_job(target: str, state: BotState) -> UploadingJob | None:
    for job in state.uploading_jobs.values():
        if job.job_id == target or job.job_id.startswith(target) or job.album_id == target:
            return job
    return None


def _truncate_display_text(text: str, max_chars: int) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 1)].rstrip() + "…"


async def _send_album_cover(
    album_id: str,
    group_id: str,
    cover_url: str,
    settings: BotSettings,
    napcat: NapCatClient,
) -> None:
    try:
        await napcat.send_group_image(group_id, cover_url)
        return
    except NapCatAPIError:
        logger.warning("Could not send album cover by URL for JM%s; trying local cache.", album_id, exc_info=True)

    try:
        cover_path = await _download_cover_image(
            cover_url,
            settings.data_dir.resolve() / "cover_cache",
            album_id,
        )
    except Exception:
        logger.exception("Could not download album cover for JM%s.", album_id)
        return

    for attempt in range(1, COVER_SEND_RETRIES + 1):
        try:
            await napcat.send_group_image(group_id, str(cover_path))
            return
        except NapCatAPIError as exc:
            if attempt < COVER_SEND_RETRIES:
                logger.warning(
                    "Local cover send attempt %s failed for JM%s: %s",
                    attempt,
                    album_id,
                    exc,
                )
                await asyncio.sleep(min(10, 2 * attempt))
            else:
                logger.exception("Could not send cached album cover for JM%s.", album_id)


async def _download_cover_image(cover_url: str, cache_dir: Path, album_id: str) -> Path:
    cache_dir = cache_dir.resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    tmp_path: Path | None = None
    try:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(COVER_DOWNLOAD_TIMEOUT_SECONDS),
            headers={"User-Agent": "qqbot-jmcomic/0.1"},
        ) as client:
            async with client.stream("GET", cover_url) as response:
                response.raise_for_status()
                extension = _cover_image_extension(response.headers.get("content-type"), cover_url)
                safe_album_id = re.sub(r"\D+", "", album_id)[:12] or "unknown"
                cover_path = (cache_dir / f"JM{safe_album_id}{extension}").resolve()
                if not cover_path.is_relative_to(cache_dir):
                    raise ValueError("Invalid cover cache path")

                tmp_path = cover_path.with_name(f"{cover_path.name}.tmp")
                size = 0
                with tmp_path.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        if not chunk:
                            continue
                        size += len(chunk)
                        if size > MAX_COVER_IMAGE_BYTES:
                            raise ValueError("Cover image is too large")
                        file.write(chunk)

        if tmp_path is None or not tmp_path.is_file() or tmp_path.stat().st_size <= 0:
            raise ValueError("Cover image is empty")
        tmp_path.replace(cover_path)
        return cover_path
    except Exception:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)
        raise


def _cover_image_extension(content_type: str | None, cover_url: str) -> str:
    normalized = (content_type or "").split(";", 1)[0].strip().lower()
    content_type_extensions = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }
    if normalized in content_type_extensions:
        return content_type_extensions[normalized]

    suffix = Path(urlsplit(cover_url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


async def _create_job_and_monitor(
    album_id: str,
    group_id: str,
    user_id: str,
    settings: BotSettings,
    napcat: NapCatClient,
    backend: BackendClient,
    spawn_task: Callable[[Awaitable[None]], None],
    state: BotState | None = None,
    page_count: int | None = None,
    extra_message: str | None = None,
) -> None:
    try:
        created = await backend.create_job(album_id, group_id, user_id, page_count=page_count)
    except DuplicateJobError as exc:
        suffix = f"：{exc.job_id}" if exc.job_id else ""
        await _safe_send(
            napcat,
            group_id,
            lang_text("duplicate_job", album_id=album_id, suffix=suffix, error_code=exc.error_code),
        )
        return
    except JobLimitError as exc:
        logger.info("Job limit rejected JM%s: %s", album_id, exc)
        await _safe_send(napcat, group_id, lang_text("job_limit_reached", error=exc, error_code=exc.error_code))
        return
    except BackendError as exc:
        logger.exception("Could not create backend job.")
        if exc.error_code == "BACKEND_UNAVAILABLE":
            await _safe_send(napcat, group_id, lang_text("backend_unavailable", error_code=exc.error_code))
        else:
            await _safe_send(napcat, group_id, lang_text("job_create_failed", error=exc, error_code=exc.error_code))
        return

    job_id = str(created["job_id"])
    message = lang_text("job_accepted", album_id=album_id, job_id=job_id)
    if extra_message:
        message = f"{message}\n{extra_message}"
    await _safe_send(napcat, group_id, message)
    spawn_task(monitor_job(job_id, album_id, group_id, settings, napcat, backend, state=state))


async def monitor_job(
    job_id: str,
    album_id: str,
    group_id: str,
    settings: BotSettings,
    napcat: NapCatClient,
    backend: BackendClient,
    state: BotState | None = None,
) -> None:
    last_progress_at = asyncio.get_running_loop().time()
    last_progress_key: tuple[str | None, str | None, int] | None = None

    while True:
        try:
            job = await backend.get_job(job_id)
        except BackendError as exc:
            logger.exception("Could not query job %s.", job_id)
            await asyncio.sleep(settings.poll_interval_seconds)
            continue

        status = job.get("status")
        if status == "failed":
            error_message = job.get("error_message") or lang_text("generic_job_failed")
            error_code = job.get("error_code") or "UNKNOWN"
            await _safe_send(
                napcat,
                group_id,
                lang_text("job_failed", album_id=album_id, error_message=error_message, error_code=error_code),
            )
            return

        if status == "completed":
            await _download_and_upload(job, album_id, group_id, settings, napcat, backend, state=state)
            return

        progress_message = job.get("progress_message")
        downloaded_files = int(job.get("downloaded_files") or 0)
        progress_key = (status, progress_message, downloaded_files)
        now = asyncio.get_running_loop().time()
        if (
            settings.progress_notify_seconds > 0
            and status != "downloading"
            and progress_message
            and progress_key != last_progress_key
            and now - last_progress_at >= settings.progress_notify_seconds
        ):
            await _safe_send(
                napcat,
                group_id,
                lang_text("job_progress", album_id=album_id, progress_message=progress_message),
            )
            last_progress_at = now
            last_progress_key = progress_key

        await asyncio.sleep(settings.poll_interval_seconds)


async def _download_and_upload(
    job: dict[str, Any],
    album_id: str,
    group_id: str,
    settings: BotSettings,
    napcat: NapCatClient,
    backend: BackendClient,
    state: BotState | None = None,
) -> None:
    job_id = str(job["job_id"])
    raw_filename = str(job.get("filename") or f"[JM{album_id}].pdf")
    filename = _safe_filename(
        raw_filename,
        f"[JM{album_id}].pdf",
    )
    upload_filename = _upload_display_filename(
        raw_filename,
        f"[JM{album_id}].pdf",
        album_id,
        settings.max_upload_filename_bytes,
    )
    dest_dir = settings.data_dir.resolve() / "bot_downloads" / job_id
    dest_path = dest_dir / filename
    if state is not None:
        state.uploading_jobs[job_id] = UploadingJob(
            job_id=job_id,
            album_id=album_id,
            group_id=group_id,
            user_id=str(job.get("user_id") or ""),
            started_at=asyncio.get_running_loop().time(),
        )

    try:
        try:
            pdf_path = await backend.download_file(job_id, dest_path)
        except BackendError as exc:
            logger.exception("Could not download PDF for job %s.", job_id)
            await _safe_send(napcat, group_id, lang_text("pdf_download_failed", album_id=album_id, error_code=exc.error_code))
            return

        try:
            upload_files = await asyncio.to_thread(
                _prepare_upload_files,
                pdf_path,
                upload_filename,
                settings.max_upload_bytes,
                settings.max_upload_filename_bytes,
                album_id,
            )
        except UploadPreparationError as exc:
            logger.exception("Could not prepare upload files for job %s.", job_id)
            await _safe_send(napcat, group_id, lang_text("upload_prepare_failed", album_id=album_id, error=exc))
            return

        if len(upload_files) > 1:
            await _safe_send(
                napcat,
                group_id,
                lang_text(
                    "large_pdf_split",
                    album_id=album_id,
                    size=_format_bytes(pdf_path.stat().st_size),
                    count=len(upload_files),
                ),
            )

        try:
            for index, (upload_path, upload_name) in enumerate(upload_files, start=1):
                if _upload_cancel_requested(state, job_id):
                    raise UploadCancelledError
                if not await _upload_item_with_fallback(
                    napcat,
                    group_id,
                    upload_path,
                    upload_name,
                    dest_dir,
                    job_id,
                    album_id,
                    settings.max_upload_filename_bytes,
                    settings.upload_retries,
                    label=f"upload_{index:02d}",
                    cancel_requested=lambda: _upload_cancel_requested(state, job_id),
                ):
                    await _safe_send(
                        napcat,
                        group_id,
                        lang_text("upload_part_failed", album_id=album_id, index=index, count=len(upload_files)),
                    )
                    return
        except UploadCancelledError:
            await _safe_send(napcat, group_id, lang_text("upload_cancelled_by_admin", album_id=album_id))
            return

        if len(upload_files) == 1:
            await _safe_send(napcat, group_id, lang_text("upload_completed", album_id=album_id, filename=filename))
        else:
            await _safe_send(napcat, group_id, lang_text("upload_completed_parts", album_id=album_id))
        await asyncio.to_thread(_cleanup_bot_download_dir, dest_dir, settings.data_dir.resolve() / "bot_downloads")
    finally:
        if state is not None:
            state.uploading_jobs.pop(job_id, None)
            state.cancelled_uploads.discard(job_id)


def _upload_cancel_requested(state: BotState | None, job_id: str) -> bool:
    return state is not None and job_id in state.cancelled_uploads


async def _upload_item_with_fallback(
    napcat: NapCatClient,
    group_id: str,
    file_path: Path,
    filename: str,
    dest_dir: Path,
    job_id: str,
    album_id: str,
    max_filename_bytes: int,
    upload_retries: int,
    label: str,
    cancel_requested: Callable[[], bool] | None = None,
    depth: int = 0,
) -> bool:
    if cancel_requested is not None and cancel_requested():
        raise UploadCancelledError
    staged_path = await asyncio.to_thread(_stage_upload_file, file_path, dest_dir, label)
    if await _upload_with_retries(
        napcat,
        group_id,
        staged_path,
        filename,
        job_id,
        upload_retries,
        cancel_requested=cancel_requested,
    ):
        return True

    compact_filename = _compact_upload_filename(album_id, filename)
    if compact_filename != filename and await _upload_with_retries(
        napcat,
        group_id,
        staged_path,
        compact_filename,
        job_id,
        upload_retries,
        cancel_requested=cancel_requested,
    ):
        return True

    if depth >= MAX_UPLOAD_FALLBACK_DEPTH:
        return False

    if file_path.stat().st_size < int(DEFAULT_MAX_UPLOAD_BYTES * 0.8):
        return False

    try:
        fallback_files = await asyncio.to_thread(
            _split_pdf_for_retry,
            staged_path,
            compact_filename,
            max_filename_bytes,
            album_id,
        )
    except UploadPreparationError:
        logger.exception("Could not split failed upload part for job %s.", job_id)
        return False

    if len(fallback_files) <= 1:
        return False

    await _safe_send(
        napcat,
        group_id,
        lang_text("upload_retry_split", album_id=album_id, count=len(fallback_files)),
    )
    for sub_index, (sub_path, sub_name) in enumerate(fallback_files, start=1):
        if cancel_requested is not None and cancel_requested():
            raise UploadCancelledError
        if not await _upload_item_with_fallback(
            napcat,
            group_id,
            sub_path,
            sub_name,
            dest_dir,
            job_id,
            album_id,
            max_filename_bytes,
            upload_retries,
            label=f"{label}_{sub_index:02d}",
            cancel_requested=cancel_requested,
            depth=depth + 1,
        ):
            return False
    return True


async def _upload_with_retries(
    napcat: NapCatClient,
    group_id: str,
    file_path: Path,
    filename: str,
    job_id: str,
    attempts: int = DEFAULT_UPLOAD_RETRIES,
    cancel_requested: Callable[[], bool] | None = None,
) -> bool:
    attempts = max(1, attempts)
    for attempt in range(1, attempts + 1):
        if cancel_requested is not None and cancel_requested():
            raise UploadCancelledError
        try:
            await napcat.upload_group_file(group_id, file_path, filename)
            return True
        except NapCatAPIError as exc:
            if attempt < attempts:
                logger.warning("Upload attempt %s failed for job %s: %s", attempt, job_id, exc)
                delay = min(60, 5 * attempt)
                if cancel_requested is None:
                    await asyncio.sleep(delay)
                else:
                    deadline = asyncio.get_running_loop().time() + delay
                    while asyncio.get_running_loop().time() < deadline:
                        if cancel_requested():
                            raise UploadCancelledError
                        await asyncio.sleep(min(1, deadline - asyncio.get_running_loop().time()))
            else:
                logger.exception("Upload attempt %s failed for job %s.", attempt, job_id)
    return False


def _prepare_upload_files(
    pdf_path: Path,
    filename: str,
    max_upload_bytes: int,
    max_filename_bytes: int = MAX_UPLOAD_FILENAME_BYTES,
    album_id: str | None = None,
) -> list[tuple[Path, str]]:
    pdf_path = pdf_path.resolve()
    if max_upload_bytes <= 0 or pdf_path.stat().st_size <= max_upload_bytes:
        return [(pdf_path, filename)]
    return _split_pdf_for_upload(pdf_path, filename, max_upload_bytes, max_filename_bytes, album_id)


def _split_pdf_for_upload(
    pdf_path: Path,
    filename: str,
    max_upload_bytes: int,
    max_filename_bytes: int = MAX_UPLOAD_FILENAME_BYTES,
    album_id: str | None = None,
) -> list[tuple[Path, str]]:
    try:
        import pikepdf
    except ImportError as exc:
        raise UploadPreparationError(lang_text("upload_error_missing_pikepdf")) from exc

    split_dir = pdf_path.parent / f"{pdf_path.stem}_parts"
    parent = pdf_path.parent.resolve()
    split_dir = split_dir.resolve()
    if not split_dir.is_relative_to(parent):
        raise UploadPreparationError(lang_text("upload_error_split_dir"))
    if split_dir.exists():
        shutil.rmtree(split_dir)
    split_dir.mkdir(parents=True, exist_ok=True)

    try:
        with pikepdf.Pdf.open(pdf_path) as source_pdf:
            page_count = len(source_pdf.pages)
            if page_count <= 1:
                return [(pdf_path, filename)]

            target_bytes = max(1, int(max_upload_bytes * 0.85))
            part_count = min(page_count, max(2, math.ceil(pdf_path.stat().st_size / target_bytes)))
            while part_count <= page_count:
                shutil.rmtree(split_dir)
                split_dir.mkdir(parents=True, exist_ok=True)
                pages_per_part = max(1, math.ceil(page_count / part_count))
                parts = _write_pdf_parts(
                    source_pdf,
                    page_count,
                    pages_per_part,
                    split_dir,
                    filename,
                    max_filename_bytes,
                    album_id,
                )
                oversized = [path for path, _name in parts if path.stat().st_size > max_upload_bytes]
                if not oversized:
                    return parts
                if pages_per_part == 1:
                    raise UploadPreparationError(lang_text("upload_error_part_too_large"))
                part_count = min(page_count, max(part_count + 1, math.ceil(part_count * 1.5)))
    except UploadPreparationError:
        raise
    except Exception as exc:
        raise UploadPreparationError(lang_text("upload_error_split_failed")) from exc

    raise UploadPreparationError(lang_text("upload_error_split_failed"))


def _write_pdf_parts(
    source_pdf: Any,
    page_count: int,
    pages_per_part: int,
    split_dir: Path,
    filename: str,
    max_filename_bytes: int,
    album_id: str | None = None,
) -> list[tuple[Path, str]]:
    parts: list[tuple[Path, str]] = []
    total = math.ceil(page_count / pages_per_part)
    start = 0
    index = 1
    while start < page_count:
        end = min(page_count, start + pages_per_part)
        part_pdf = None
        part_path: Path | None = None
        try:
            import pikepdf

            part_pdf = pikepdf.Pdf.new()
            for page_index in range(start, end):
                part_pdf.pages.append(source_pdf.pages[page_index])

            part_name = _part_filename(filename, index, total, max_filename_bytes, album_id)
            part_path = split_dir / part_name
            part_pdf.save(part_path)
        finally:
            if part_pdf is not None:
                part_pdf.close()

        if part_path is None or not part_path.is_file() or part_path.stat().st_size <= 0:
            raise UploadPreparationError(lang_text("upload_error_invalid_part"))
        parts.append((part_path, part_name))
        start = end
        index += 1
    return parts


def _split_pdf_for_retry(
    pdf_path: Path,
    filename: str,
    max_filename_bytes: int,
    album_id: str | None = None,
) -> list[tuple[Path, str]]:
    pdf_path = pdf_path.resolve()
    if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
        raise UploadPreparationError(lang_text("upload_error_invalid_part"))
    retry_max_upload_bytes = max(1, int(pdf_path.stat().st_size * 0.65))
    return _split_pdf_for_upload(pdf_path, filename, retry_max_upload_bytes, max_filename_bytes, album_id)


def _stage_upload_file(source_path: Path, dest_dir: Path, label: str) -> Path:
    source_path = source_path.resolve()
    if not source_path.is_file() or source_path.stat().st_size <= 0:
        raise UploadPreparationError(lang_text("upload_error_invalid_part"))

    dest_dir = dest_dir.resolve()
    stage_dir = (dest_dir / "_upload").resolve()
    if not stage_dir.is_relative_to(dest_dir):
        raise UploadPreparationError(lang_text("upload_error_split_dir"))
    stage_dir.mkdir(parents=True, exist_ok=True)

    safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "_", label).strip("._") or "upload"
    staged_path = stage_dir / f"{safe_label}.pdf"
    if staged_path.exists():
        staged_path.unlink()
    shutil.copy2(source_path, staged_path)
    if staged_path.stat().st_size != source_path.stat().st_size:
        staged_path.unlink(missing_ok=True)
        raise UploadPreparationError(lang_text("upload_error_invalid_part"))
    return staged_path


def _cleanup_bot_download_dir(dest_dir: Path, bot_downloads_dir: Path) -> None:
    dest_dir = dest_dir.resolve()
    bot_downloads_dir = bot_downloads_dir.resolve()
    if dest_dir == bot_downloads_dir or not dest_dir.is_relative_to(bot_downloads_dir):
        logger.warning("Skip bot download cleanup outside cache dir: %s", dest_dir)
        return
    if not dest_dir.exists():
        return
    try:
        shutil.rmtree(dest_dir)
    except OSError:
        logger.exception("Could not cleanup bot download cache: %s", dest_dir)


def _part_filename(
    filename: str,
    index: int,
    total: int,
    max_filename_bytes: int = MAX_UPLOAD_FILENAME_BYTES,
    album_id: str | None = None,
) -> str:
    album = album_id or _album_id_from_filename(filename)
    if album:
        return f"JM{album}_part{index:02d}-of{total:02d}.pdf"

    safe = _safe_filename(filename, "upload.pdf", max_bytes=max_filename_bytes)
    stem = Path(safe).stem.strip(" .")
    if len(stem) > 120:
        stem = stem[:120].strip(" .")
    stem = stem or "upload"
    return _safe_filename(
        f"part{index:02d}-of{total:02d}_{stem}.pdf",
        f"part{index:02d}-of{total:02d}.pdf",
        max_bytes=max_filename_bytes,
    )


def _album_id_from_filename(filename: str) -> str | None:
    match = re.search(r"(?i)JM\s*(\d{1,12})", filename)
    return match.group(1) if match else None


def _compact_upload_filename(album_id: str, filename: str) -> str:
    match = re.match(r"(?:JM\d+_)?part(\d+)-of(\d+)", filename)
    if match:
        return f"JM{album_id}_part{int(match.group(1)):02d}-of{int(match.group(2)):02d}.pdf"
    return f"JM{album_id}.pdf"


def _upload_display_filename(filename: str, fallback: str, album_id: str, max_bytes: int) -> str:
    _safe_filename(filename, fallback, max_bytes=max_bytes)
    return _compact_upload_filename(album_id, filename)


def _format_bytes(size: int) -> str:
    if size <= 0:
        return "0B"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    unit_index = 0
    while value >= 1024 and unit_index < len(units) - 1:
        value /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(value)}B"
    if value >= 10:
        return f"{value:.0f}{units[unit_index]}"
    return f"{value:.1f}{units[unit_index]}"


def _format_rate(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "未知"
    return f"{_format_bytes(int(value))}/s"


async def _safe_send(napcat: NapCatClient, group_id: str, message: str) -> None:
    try:
        await napcat.send_group_msg(group_id, message)
    except NapCatAPIError:
        logger.exception("Could not send group message.")


def _spawn_task(pending_tasks: set[asyncio.Task[None]], awaitable: Awaitable[None]) -> None:
    task = asyncio.create_task(awaitable)
    pending_tasks.add(task)

    def _done(done_task: asyncio.Task[None]) -> None:
        pending_tasks.discard(done_task)
        try:
            done_task.result()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Background bot task failed.")

    task.add_done_callback(_done)


async def run_bot() -> None:
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = BotSettings.from_env()
    settings.data_dir.mkdir(parents=True, exist_ok=True)

    pending_tasks: set[asyncio.Task[None]] = set()
    state = BotState()
    async with NapCatClient(
        settings.napcat_ws_url,
        settings.napcat_http_url,
        settings.napcat_access_token,
        request_timeout_seconds=settings.napcat_http_timeout_seconds,
        upload_timeout_seconds=settings.napcat_upload_timeout_seconds,
    ) as napcat, BackendClient(
        settings.backend_url,
        settings.backend_api_token,
    ) as backend:
        try:
            async for event in napcat.iter_events():
                _spawn_task(
                    pending_tasks,
                    handle_group_message(
                        event,
                        settings,
                        state,
                        napcat,
                        backend,
                        lambda awaitable: _spawn_task(pending_tasks, awaitable),
                    ),
                )
        finally:
            tasks = list(pending_tasks)
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def main() -> None:
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")


if __name__ == "__main__":
    main()
