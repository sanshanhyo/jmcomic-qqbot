from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .backend_client import BackendClient, BackendError, DuplicateJobError
from .message_parser import ParseAction, parse_group_message, text_from_segments
from .napcat_client import NapCatAPIError, NapCatClient

logger = logging.getLogger(__name__)

USAGE_MESSAGE = "用法：@机器人 JM123456"
ILLEGAL_FILENAME_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
CONFIRM_WORDS = {"下载", "确认", "同意", "是", "要", "y", "yes", "ok"}
CANCEL_WORDS = {"取消", "取消下载", "取消任务", "不要", "否", "不下", "n", "no"}
ACTIVE_CANCEL_WORDS = CANCEL_WORDS | {"取消下载", "取消任务", "停止下载", "停止任务"}


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
    progress_notify_seconds: int = 60
    confirm_timeout_seconds: int = 300

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
            progress_notify_seconds=max(10, _env_int("JOB_PROGRESS_NOTIFY_SECONDS", 60)),
            confirm_timeout_seconds=max(30, _env_int("JOB_CONFIRM_TIMEOUT_SECONDS", 300)),
        )


@dataclass(frozen=True)
class PendingDownload:
    album_id: str
    title: str
    estimated_text: str
    expires_at: float


@dataclass
class BotState:
    pending_downloads: dict[tuple[str, str], PendingDownload] = field(default_factory=dict)

    def cleanup(self, now: float) -> None:
        expired = [
            key
            for key, pending in self.pending_downloads.items()
            if pending.expires_at <= now
        ]
        for key in expired:
            self.pending_downloads.pop(key, None)


def _safe_filename(name: str, fallback: str) -> str:
    cleaned = ILLEGAL_FILENAME_CHARS_RE.sub("_", name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned or fallback


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

    now = asyncio.get_running_loop().time()
    state.cleanup(now)
    if await _handle_pending_confirmation(event, group_id, user_id, settings, state, napcat, backend, spawn_task):
        return
    if await _handle_active_cancel(event, group_id, user_id, napcat, backend):
        return

    parse_result = parse_group_message(event, settings.bot_qq_id)
    if parse_result.action == ParseAction.IGNORE:
        return

    if parse_result.action == ParseAction.USAGE:
        await _safe_send(napcat, group_id, USAGE_MESSAGE)
        return

    if parse_result.action == ParseAction.ERROR:
        await _safe_send(napcat, group_id, parse_result.error_message or USAGE_MESSAGE)
        return

    album_id = parse_result.album_id
    if album_id is None:
        await _safe_send(napcat, group_id, USAGE_MESSAGE)
        return

    try:
        active = await backend.get_active_job(group_id, user_id)
    except BackendError:
        logger.exception("Could not query active job for group=%s user=%s.", group_id, user_id)
        await _safe_send(napcat, group_id, "后端暂不可用，请稍后再试")
        return

    if active is not None:
        await _safe_send(
            napcat,
            group_id,
            f"你已有 JM{active.get('album_id')} 正在下载或排队中，回复“取消下载”可以停止当前任务。",
        )
        return

    await _send_album_preview(album_id, group_id, user_id, settings, state, napcat, backend)


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

    if text in CANCEL_WORDS:
        state.pending_downloads.pop(key, None)
        await _safe_send(napcat, group_id, f"已取消 JM{pending.album_id}。")
        return True

    if text not in CONFIRM_WORDS:
        return False

    state.pending_downloads.pop(key, None)
    await _create_job_and_monitor(
        pending.album_id,
        group_id,
        user_id,
        settings,
        napcat,
        backend,
        spawn_task,
        extra_message=f"预计时间：{pending.estimated_text}",
    )
    return True


async def _handle_active_cancel(
    event: dict[str, Any],
    group_id: str,
    user_id: str,
    napcat: NapCatClient,
    backend: BackendClient,
) -> bool:
    text = text_from_segments(event.get("message")).strip().lower()
    if text not in ACTIVE_CANCEL_WORDS:
        return False

    try:
        cancelled = await backend.cancel_active_job(group_id, user_id)
    except BackendError:
        logger.exception("Could not cancel active job for group=%s user=%s.", group_id, user_id)
        await _safe_send(napcat, group_id, "取消失败，请稍后再试")
        return True

    if cancelled is None:
        await _safe_send(napcat, group_id, "你没有正在进行的任务。")
        return True

    await _safe_send(napcat, group_id, f"已取消 JM{cancelled.get('album_id')} 任务。")
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
        await _safe_send(napcat, group_id, f"JM{album_id} 获取信息失败：{exc}")
        return

    title = str(preview.get("title") or f"JM{album_id}")
    estimated_text = str(preview.get("estimated_text") or "预计时间未知")
    cover_url = preview.get("cover_url")
    page_count = preview.get("page_count")

    if isinstance(cover_url, str) and cover_url:
        try:
            await napcat.send_group_image(group_id, cover_url)
        except NapCatAPIError:
            logger.exception("Could not send album cover.")

    page_text = f"{page_count} 页" if isinstance(page_count, int) and page_count > 0 else "页数未知"
    await _safe_send(
        napcat,
        group_id,
        (
            f"JM{album_id}\n"
            f"标题：{title}\n"
            f"页数：{page_text}\n"
            f"预计时间：{estimated_text}\n"
            f"回复“下载”确认加入队列，回复“取消”放弃。"
        ),
    )
    state.pending_downloads[(group_id, user_id)] = PendingDownload(
        album_id=album_id,
        title=title,
        estimated_text=estimated_text,
        expires_at=asyncio.get_running_loop().time() + settings.confirm_timeout_seconds,
    )


async def _create_job_and_monitor(
    album_id: str,
    group_id: str,
    user_id: str,
    settings: BotSettings,
    napcat: NapCatClient,
    backend: BackendClient,
    spawn_task: Callable[[Awaitable[None]], None],
    extra_message: str | None = None,
) -> None:
    try:
        created = await backend.create_job(album_id, group_id, user_id)
    except DuplicateJobError as exc:
        suffix = f"：{exc.job_id}" if exc.job_id else ""
        await _safe_send(napcat, group_id, f"JM{album_id} 已有进行中的任务{suffix}")
        return
    except BackendError:
        logger.exception("Could not create backend job.")
        await _safe_send(napcat, group_id, "后端暂不可用，请稍后再试")
        return

    job_id = str(created["job_id"])
    message = f"已接收 JM{album_id}，任务编号：{job_id}"
    if extra_message:
        message = f"{message}\n{extra_message}"
    await _safe_send(napcat, group_id, message)
    spawn_task(monitor_job(job_id, album_id, group_id, settings, napcat, backend))


async def monitor_job(
    job_id: str,
    album_id: str,
    group_id: str,
    settings: BotSettings,
    napcat: NapCatClient,
    backend: BackendClient,
) -> None:
    last_progress_at = asyncio.get_running_loop().time()
    last_progress_key: tuple[str | None, str | None, int] | None = None

    while True:
        try:
            job = await backend.get_job(job_id)
        except BackendError:
            logger.exception("Could not query job %s.", job_id)
            await asyncio.sleep(settings.poll_interval_seconds)
            continue

        status = job.get("status")
        if status == "failed":
            error_message = job.get("error_message") or "任务失败，请稍后重试"
            await _safe_send(napcat, group_id, f"JM{album_id} 任务失败：{error_message}")
            return

        if status == "completed":
            await _download_and_upload(job, album_id, group_id, settings, napcat, backend)
            return

        progress_message = job.get("progress_message")
        downloaded_files = int(job.get("downloaded_files") or 0)
        progress_key = (status, progress_message, downloaded_files)
        now = asyncio.get_running_loop().time()
        if (
            progress_message
            and progress_key != last_progress_key
            and now - last_progress_at >= settings.progress_notify_seconds
        ):
            await _safe_send(napcat, group_id, f"JM{album_id} 进度：{progress_message}")
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
) -> None:
    job_id = str(job["job_id"])
    filename = _safe_filename(str(job.get("filename") or f"[JM{album_id}].pdf"), f"[JM{album_id}].pdf")
    dest_dir = settings.data_dir.resolve() / "bot_downloads" / job_id
    dest_path = dest_dir / filename

    try:
        pdf_path = await backend.download_file(job_id, dest_path)
    except BackendError:
        logger.exception("Could not download PDF for job %s.", job_id)
        await _safe_send(napcat, group_id, f"JM{album_id} PDF 下载失败，请稍后重试")
        return

    for attempt in range(1, 4):
        try:
            await napcat.upload_group_file(group_id, pdf_path, filename)
            await _safe_send(napcat, group_id, f"JM{album_id} 已完成，PDF 已上传：{filename}")
            return
        except NapCatAPIError:
            logger.exception("Upload attempt %s failed for job %s.", attempt, job_id)
            if attempt < 3:
                await asyncio.sleep(attempt * 2)

    await _safe_send(napcat, group_id, f"JM{album_id} 已完成，但上传文件失败，请稍后重试")


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
    asyncio.run(run_bot())


if __name__ == "__main__":
    main()
