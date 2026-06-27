from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .models import JobStatus

logger = logging.getLogger(__name__)


class ErrorCode:
    USER_CANCELLED = "USER_CANCELLED"
    JOB_TIMEOUT = "JOB_TIMEOUT"
    JOB_STALLED = "JOB_STALLED"
    JM_DOWNLOAD_FAILED = "JM_DOWNLOAD_FAILED"
    JM_NOT_FOUND = "JM_NOT_FOUND"
    PDF_GENERATION_FAILED = "PDF_GENERATION_FAILED"
    PDF_INVALID = "PDF_INVALID"
    PDF_OUTPUT_PATH_INVALID = "PDF_OUTPUT_PATH_INVALID"
    WORKER_EXITED = "WORKER_EXITED"
    WORKER_RESULT_INVALID = "WORKER_RESULT_INVALID"
    WORKER_UNEXPECTED = "WORKER_UNEXPECTED"
    JOB_UNEXPECTED = "JOB_UNEXPECTED"


class DuplicateJobError(Exception):
    def __init__(self, existing_job: dict[str, Any]) -> None:
        super().__init__("duplicate active job")
        self.existing_job = existing_job


class ActiveJobLimitError(Exception):
    def __init__(self, user_message: str, error_code: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.error_code = error_code


class DownloadWorkerError(Exception):
    def __init__(self, user_message: str, error_code: str = ErrorCode.JM_DOWNLOAD_FAILED) -> None:
        super().__init__(user_message)
        self.user_message = user_message
        self.error_code = error_code


@dataclass(frozen=True)
class JobActivitySnapshot:
    file_count: int
    total_size: int
    latest_mtime_ns: int


@dataclass(frozen=True)
class JobManagerConfig:
    data_dir: Path
    option_path: Path
    max_concurrent_jobs: int = 1
    job_timeout_seconds: int = 1800
    job_stall_timeout_seconds: int = 300
    progress_interval_seconds: float = 10.0
    cache_cleanup_interval_seconds: int = 3600
    job_cache_ttl_seconds: int = 259200
    bot_download_cache_ttl_seconds: int = 259200
    preview_cache_ttl_seconds: int = 86400
    max_active_jobs_per_group: int = 3
    max_active_jobs_per_user: int = 1


class JobManager:
    IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

    def __init__(self, config: JobManagerConfig) -> None:
        self.config = config
        self.data_dir = config.data_dir.resolve()
        self.jobs_dir = self.data_dir / "jobs"
        self.bot_downloads_dir = self.data_dir / "bot_downloads"
        self.previews_dir = self.data_dir / "previews"
        self.db_path = self.data_dir / "jobs.sqlite3"
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._active_processes: dict[str, asyncio.subprocess.Process] = {}
        self._termination_tasks: set[asyncio.Task[None]] = set()
        self._cleanup_task: asyncio.Task[None] | None = None

    def initialize(self) -> None:
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    album_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    filename TEXT,
                    file_path TEXT,
                    error_message TEXT,
                    error_code TEXT,
                    downloaded_files INTEGER NOT NULL DEFAULT 0,
                    total_files INTEGER NOT NULL DEFAULT 0,
                    progress_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "error_code", "TEXT")
            self._ensure_column(conn, "downloaded_files", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "total_files", "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column(conn, "progress_message", "TEXT")
            conn.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active_album_group
                ON jobs(album_id, group_id)
                WHERE status IN ('queued', 'downloading', 'converting')
                """
            )
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, updated_at = ?
                WHERE status IN (?, ?)
                """,
                (
                    JobStatus.QUEUED.value,
                    self._now(),
                    JobStatus.DOWNLOADING.value,
                    JobStatus.CONVERTING.value,
                ),
            )

    async def start(self) -> None:
        self.initialize()
        for job_id in self._queued_job_ids():
            await self._queue.put(job_id)

        worker_count = max(1, self.config.max_concurrent_jobs)
        self._workers = [
            asyncio.create_task(self._worker(worker_id), name=f"job-worker-{worker_id}")
            for worker_id in range(worker_count)
        ]
        if self.config.cache_cleanup_interval_seconds > 0:
            self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="cache-cleanup")

    async def stop(self) -> None:
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            await asyncio.gather(self._cleanup_task, return_exceptions=True)
            self._cleanup_task = None

        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        termination_tasks = list(self._termination_tasks)
        for task in termination_tasks:
            task.cancel()
        await asyncio.gather(*termination_tasks, return_exceptions=True)
        self._termination_tasks.clear()

    async def join(self) -> None:
        await self._queue.join()

    async def cleanup_cache_once(self) -> dict[str, int]:
        return await asyncio.to_thread(self._cleanup_cache_sync)

    async def create_job(self, album_id: str, group_id: str, user_id: str, page_count: int | None = None) -> dict[str, Any]:
        existing = self.find_active_job(album_id, group_id)
        if existing is not None:
            raise DuplicateJobError(existing)

        if (
            self.config.max_active_jobs_per_group > 0
            and self.count_active_jobs_for_group(group_id) >= self.config.max_active_jobs_per_group
        ):
            raise ActiveJobLimitError(
                f"本群当前任务已满（最多 {self.config.max_active_jobs_per_group} 个），请稍后再试",
                "GROUP_ACTIVE_JOB_LIMIT",
            )

        if (
            self.config.max_active_jobs_per_user > 0
            and self.count_active_jobs_for_user(user_id) >= self.config.max_active_jobs_per_user
        ):
            raise ActiveJobLimitError(
                f"你已经有正在进行的任务啦（最多 {self.config.max_active_jobs_per_user} 个）",
                "USER_ACTIVE_JOB_LIMIT",
            )

        now = self._now()
        job_id = str(uuid.uuid4())
        total_files = max(0, int(page_count or 0))
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id, album_id, group_id, user_id, status,
                        filename, file_path, error_message, error_code, downloaded_files,
                        total_files, progress_message, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, 0, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        album_id,
                        group_id,
                        user_id,
                        JobStatus.QUEUED.value,
                        total_files,
                        "排队中，等待下载 worker 处理",
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            existing = self.find_active_job(album_id, group_id)
            if existing is not None:
                raise DuplicateJobError(existing) from exc
            raise

        self._queue.put_nowait(job_id)
        created = self.get_job(job_id)
        if created is None:
            raise RuntimeError("created job disappeared")
        return created

    async def cancel_active_job_for_user(
        self,
        group_id: str,
        user_id: str,
        reason: str = "任务已取消",
    ) -> dict[str, Any] | None:
        job = self.find_active_job_for_user(group_id=group_id, user_id=user_id)
        if job is None:
            return None
        return await self.cancel_job(str(job["job_id"]), reason)

    async def cancel_job(self, job_id: str, reason: str = "任务已取消") -> dict[str, Any] | None:
        job = self.get_job(job_id)
        if job is None:
            return None

        if job["status"] in {JobStatus.COMPLETED.value, JobStatus.FAILED.value}:
            return job

        self._mark_failed(job_id, reason, ErrorCode.USER_CANCELLED)
        process = self._active_processes.get(job_id)
        if process is not None:
            self._terminate_download_process_soon(job_id, process)

        cancelled = self.get_job(job_id)
        if cancelled is None:
            raise RuntimeError("cancelled job disappeared")
        return cancelled

    def _terminate_download_process_soon(self, job_id: str, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return

        task = asyncio.create_task(
            self._terminate_download_process(process),
            name=f"terminate-download-{job_id}",
        )
        self._termination_tasks.add(task)

        def _done(done_task: asyncio.Task[None]) -> None:
            self._termination_tasks.discard(done_task)
            try:
                done_task.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("Background termination failed for job %s.", job_id)

        task.add_done_callback(_done)

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT job_id, album_id, group_id, user_id, status,
                       filename, file_path, error_message, error_code, downloaded_files,
                       total_files, progress_message, created_at, updated_at
                FROM jobs
                WHERE job_id = ?
                """,
                (job_id,),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def get_completed_file(self, job_id: str) -> tuple[Path, str] | None:
        job = self.get_job(job_id)
        if job is None or job["status"] != JobStatus.COMPLETED.value:
            return None
        file_path = job.get("file_path")
        filename = job.get("filename")
        if not file_path or not filename:
            return None
        path = Path(file_path).resolve()
        if not path.is_file() or not path.is_relative_to(self.jobs_dir.resolve()):
            return None
        return path, filename

    def find_active_job_for_user(self, group_id: str | None = None, user_id: str | None = None) -> dict[str, Any] | None:
        if user_id is None:
            return None
        where = ["user_id = ?", "status IN (?, ?, ?)"]
        params: list[object] = [
            user_id,
            JobStatus.QUEUED.value,
            JobStatus.DOWNLOADING.value,
            JobStatus.CONVERTING.value,
        ]
        if group_id is not None:
            where.insert(0, "group_id = ?")
            params.insert(0, group_id)
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT job_id, album_id, group_id, user_id, status,
                       filename, file_path, error_message, error_code, downloaded_files,
                       total_files, progress_message, created_at, updated_at
                FROM jobs
                WHERE {' AND '.join(where)}
                ORDER BY created_at ASC
                LIMIT 1
                """,
                tuple(params),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def count_active_jobs_for_group(self, group_id: str) -> int:
        return self._count_active_jobs("group_id", group_id)

    def count_active_jobs_for_user(self, user_id: str) -> int:
        return self._count_active_jobs("user_id", user_id)

    def count_active_jobs(self) -> int:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM jobs
                WHERE status IN (?, ?, ?)
                """,
                (
                    JobStatus.QUEUED.value,
                    JobStatus.DOWNLOADING.value,
                    JobStatus.CONVERTING.value,
                ),
            ).fetchone()
        return int(row["count"] or 0) if row else 0

    def list_admin_jobs(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(50, int(limit)))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT job_id, album_id, group_id, user_id, status,
                       filename, file_path, error_message, error_code, downloaded_files,
                       total_files, progress_message, created_at, updated_at
                FROM jobs
                WHERE status IN (?, ?, ?, ?)
                ORDER BY
                    CASE status
                        WHEN ? THEN 0
                        WHEN ? THEN 1
                        WHEN ? THEN 2
                        WHEN ? THEN 3
                        ELSE 4
                    END,
                    updated_at DESC
                LIMIT ?
                """,
                (
                    JobStatus.QUEUED.value,
                    JobStatus.DOWNLOADING.value,
                    JobStatus.CONVERTING.value,
                    JobStatus.FAILED.value,
                    JobStatus.DOWNLOADING.value,
                    JobStatus.CONVERTING.value,
                    JobStatus.QUEUED.value,
                    JobStatus.FAILED.value,
                    limit,
                ),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def find_job_by_prefix(self, target: str) -> dict[str, Any] | None:
        target = target.strip()
        if not target:
            return None
        if target.isdigit():
            with self._connect() as conn:
                rows = conn.execute(
                    """
                    SELECT job_id, album_id, group_id, user_id, status,
                           filename, file_path, error_message, error_code, downloaded_files,
                           total_files, progress_message, created_at, updated_at
                    FROM jobs
                    WHERE album_id = ?
                      AND status IN (?, ?, ?)
                    ORDER BY created_at DESC
                    LIMIT 2
                    """,
                    (
                        target,
                        JobStatus.QUEUED.value,
                        JobStatus.DOWNLOADING.value,
                        JobStatus.CONVERTING.value,
                    ),
                ).fetchall()
            if len(rows) == 1:
                return self._row_to_dict(rows[0])
            if len(rows) > 1:
                return None

        if len(target) >= 32:
            job = self.get_job(target)
            if job is not None:
                return job

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT job_id, album_id, group_id, user_id, status,
                       filename, file_path, error_message, error_code, downloaded_files,
                       total_files, progress_message, created_at, updated_at
                FROM jobs
                WHERE job_id LIKE ?
                ORDER BY created_at DESC
                LIMIT 2
                """,
                (f"{target}%",),
            ).fetchall()
        if len(rows) != 1:
            return None
        return self._row_to_dict(rows[0])

    def _count_active_jobs(self, column: str, value: str) -> int:
        if column not in {"group_id", "user_id"}:
            raise ValueError("invalid active job counter")
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM jobs
                WHERE {column} = ?
                  AND status IN (?, ?, ?)
                """,
                (
                    value,
                    JobStatus.QUEUED.value,
                    JobStatus.DOWNLOADING.value,
                    JobStatus.CONVERTING.value,
                ),
            ).fetchone()
        return int(row["count"] or 0) if row else 0

    def find_active_job(self, album_id: str, group_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT job_id, album_id, group_id, user_id, status,
                       filename, file_path, error_message, error_code, downloaded_files,
                       total_files, progress_message, created_at, updated_at
                FROM jobs
                WHERE album_id = ?
                  AND group_id = ?
                  AND status IN (?, ?, ?)
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (
                    album_id,
                    group_id,
                    JobStatus.QUEUED.value,
                    JobStatus.DOWNLOADING.value,
                    JobStatus.CONVERTING.value,
                ),
            ).fetchone()
        return self._row_to_dict(row) if row else None

    async def _worker(self, worker_id: int) -> None:
        logger.info("Job worker %s started.", worker_id)
        while True:
            job_id = await self._queue.get()
            try:
                await self._process_job(job_id)
            except Exception:
                logger.exception("Unexpected worker failure for job %s", job_id)
                self._mark_failed(job_id, "任务执行失败，请查看服务日志", ErrorCode.WORKER_UNEXPECTED)
            finally:
                self._queue.task_done()

    async def _cleanup_loop(self) -> None:
        while True:
            try:
                stats = await self.cleanup_cache_once()
                removed = sum(stats.values())
                if removed:
                    logger.info("Cache cleanup removed %s item(s): %s", removed, stats)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Cache cleanup failed.")
            await asyncio.sleep(self.config.cache_cleanup_interval_seconds)

    def _cleanup_cache_sync(self) -> dict[str, int]:
        now = datetime.now(timezone.utc)
        stats = {"job_dirs": 0, "bot_downloads": 0, "previews": 0}
        active_statuses = {JobStatus.QUEUED.value, JobStatus.DOWNLOADING.value, JobStatus.CONVERTING.value}

        with self._connect() as conn:
            rows = conn.execute("SELECT job_id, status, updated_at FROM jobs").fetchall()

        known_job_ids = {str(row["job_id"]) for row in rows}
        active_job_ids = {str(row["job_id"]) for row in rows if row["status"] in active_statuses}
        job_cutoff = now - timedelta(seconds=max(0, self.config.job_cache_ttl_seconds))

        for row in rows:
            job_id = str(row["job_id"])
            if job_id in active_job_ids:
                continue
            updated_at = self._parse_datetime(str(row["updated_at"]))
            if updated_at is not None and updated_at <= job_cutoff:
                if self._remove_child_tree(self.jobs_dir / job_id, self.jobs_dir):
                    stats["job_dirs"] += 1

        if self.jobs_dir.exists():
            for path in self.jobs_dir.iterdir():
                if not path.is_dir() or path.name in known_job_ids or path.name in active_job_ids:
                    continue
                if self._path_mtime_before(path, job_cutoff) and self._remove_child_tree(path, self.jobs_dir):
                    stats["job_dirs"] += 1

        bot_cutoff = now - timedelta(seconds=max(0, self.config.bot_download_cache_ttl_seconds))
        stats["bot_downloads"] += self._cleanup_children_older_than(self.bot_downloads_dir, bot_cutoff)

        preview_cutoff = now - timedelta(seconds=max(0, self.config.preview_cache_ttl_seconds))
        stats["previews"] += self._cleanup_children_older_than(self.previews_dir, preview_cutoff)

        return stats

    async def _process_job(self, job_id: str) -> None:
        job = self.get_job(job_id)
        if job is None:
            logger.warning("Ignoring missing job %s", job_id)
            return
        if job["status"] not in {JobStatus.QUEUED.value, JobStatus.DOWNLOADING.value}:
            return

        album_id = job["album_id"]
        job_dir = self.jobs_dir / job_id
        self._update_status(job_id, JobStatus.DOWNLOADING, "开始下载，正在获取本子信息")

        try:
            pdf_path = await self._run_download_process_with_progress(
                job_id,
                album_id,
                job_dir,
                self.config.progress_interval_seconds,
            )
            if self._has_terminal_status(job_id):
                return
            self._update_status(job_id, JobStatus.CONVERTING, "图片下载完成，正在生成 PDF")
            self._mark_completed(job_id, pdf_path)
        except asyncio.TimeoutError:
            if self._has_terminal_status(job_id):
                return
            logger.exception("Job %s timed out.", job_id)
            self._mark_failed(job_id, "下载超时，请稍后重试", ErrorCode.JOB_TIMEOUT)
        except DownloadWorkerError as exc:
            if self._has_terminal_status(job_id):
                return
            logger.warning("Job %s failed in download worker: %s", job_id, exc.user_message)
            self._mark_failed(job_id, exc.user_message, exc.error_code)
        except Exception:
            logger.exception("Job %s failed unexpectedly.", job_id)
            self._mark_failed(job_id, "下载或转换失败，请查看服务日志", ErrorCode.JOB_UNEXPECTED)

    def _mark_completed(self, job_id: str, pdf_path: Path) -> None:
        pdf_path = pdf_path.resolve()
        if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
            self._mark_failed(job_id, "PDF 生成失败：最终文件无效", ErrorCode.PDF_INVALID)
            return
        if not pdf_path.is_relative_to(self.jobs_dir.resolve()):
            self._mark_failed(job_id, "PDF 生成失败：输出路径异常", ErrorCode.PDF_OUTPUT_PATH_INVALID)
            return

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, filename = ?, file_path = ?, error_message = NULL, error_code = NULL, updated_at = ?
                    , progress_message = ?
                WHERE job_id = ?
                """,
                (
                    JobStatus.COMPLETED.value,
                    pdf_path.name,
                    str(pdf_path),
                    self._now(),
                    "PDF 已生成，等待机器人上传",
                    job_id,
                ),
            )
        self._render_console_progress_from_job(job_id, "completed", final=True)

    def _mark_failed(self, job_id: str, error_message: str, error_code: str = ErrorCode.JOB_UNEXPECTED) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = ?, error_code = ?, updated_at = ?
                    , progress_message = ?
                WHERE job_id = ?
                """,
                (JobStatus.FAILED.value, error_message, error_code, self._now(), error_message, job_id),
            )
        self._render_console_progress_from_job(job_id, "failed", final=True, error_code=error_code)
        logger.warning("Job %s failed with error_code=%s: %s", job_id, error_code, error_message)

    async def _run_download_process_with_progress(
        self,
        job_id: str,
        album_id: str,
        job_dir: Path,
        progress_interval_seconds: float = 10.0,
    ) -> Path:
        job_dir.mkdir(parents=True, exist_ok=True)
        result_path = job_dir / "download-result.json"
        result_path.unlink(missing_ok=True)

        command = self._download_worker_command(album_id, job_dir, result_path)
        process = await self._start_download_process(command)
        self._active_processes[job_id] = process
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.job_timeout_seconds
        last_activity = self._activity_snapshot(job_dir)
        last_activity_at = loop.time()

        try:
            if self._has_terminal_status(job_id):
                await self._terminate_download_process(process)
                raise DownloadWorkerError("任务已取消", ErrorCode.USER_CANCELLED)

            while process.returncode is None:
                if self._has_terminal_status(job_id):
                    await self._terminate_download_process(process)
                    raise DownloadWorkerError("任务已取消", ErrorCode.USER_CANCELLED)

                remaining = deadline - loop.time()
                if remaining <= 0:
                    await self._terminate_download_process(process)
                    raise asyncio.TimeoutError

                try:
                    await asyncio.wait_for(
                        process.wait(),
                        timeout=min(progress_interval_seconds, remaining),
                    )
                except asyncio.TimeoutError:
                    now = loop.time()
                    current_activity = self._activity_snapshot(job_dir)
                    if current_activity != last_activity:
                        last_activity = current_activity
                        last_activity_at = now

                    idle_seconds = max(0.0, now - last_activity_at)
                    self._update_download_progress(job_id, job_dir, idle_seconds)
                    if (
                        self.config.job_stall_timeout_seconds > 0
                        and idle_seconds >= self.config.job_stall_timeout_seconds
                    ):
                        await self._terminate_download_process(process)
                        raise DownloadWorkerError(
                            "下载卡住：超过 "
                            f"{self._format_duration(self.config.job_stall_timeout_seconds)}"
                            "没有新文件写入，已自动终止",
                            ErrorCode.JOB_STALLED,
                        )
                    continue

            self._update_download_progress(job_id, job_dir, 0.0)
            return self._read_download_result(result_path, process.returncode)
        except asyncio.CancelledError:
            await self._terminate_download_process(process)
            raise
        finally:
            self._active_processes.pop(job_id, None)

    def _download_worker_command(self, album_id: str, job_dir: Path, result_path: Path) -> list[str]:
        return [
            sys.executable,
            "-m",
            "backend.download_worker",
            "--album-id",
            album_id,
            "--option-path",
            str(self.config.option_path),
            "--job-dir",
            str(job_dir),
            "--result-path",
            str(result_path),
        ]

    async def _start_download_process(self, command: list[str]) -> asyncio.subprocess.Process:
        kwargs: dict[str, Any] = {
            "stdout": asyncio.subprocess.DEVNULL,
            "stderr": asyncio.subprocess.DEVNULL,
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        return await asyncio.create_subprocess_exec(*command, **kwargs)

    async def _terminate_download_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return

        logger.warning("Terminating stuck download process pid=%s.", process.pid)
        try:
            if os.name == "nt":
                process.terminate()
            else:
                os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return

        try:
            await asyncio.wait_for(process.wait(), timeout=5)
            return
        except asyncio.TimeoutError:
            logger.warning("Killing stuck download process pid=%s.", process.pid)

        try:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return

        await process.wait()

    def _read_download_result(self, result_path: Path, returncode: int | None) -> Path:
        if not result_path.is_file():
            raise DownloadWorkerError(f"下载进程异常退出，退出码：{returncode}", ErrorCode.WORKER_EXITED)

        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise DownloadWorkerError("下载进程结果文件无效", ErrorCode.WORKER_RESULT_INVALID) from exc

        if not result.get("ok"):
            raise DownloadWorkerError(result.get("user_message") or "下载失败，请稍后重试", self._worker_error_code(result))

        pdf_path = result.get("pdf_path")
        if not isinstance(pdf_path, str) or not pdf_path:
            raise DownloadWorkerError("PDF 生成失败：结果路径无效", ErrorCode.PDF_GENERATION_FAILED)
        return Path(pdf_path).resolve()

    @staticmethod
    def _worker_error_code(result: dict[str, Any]) -> str:
        error_type = str(result.get("error_type") or "")
        if error_type == "AlbumNotFoundError":
            return ErrorCode.JM_NOT_FOUND
        if error_type == "PdfGenerationError":
            return ErrorCode.PDF_GENERATION_FAILED
        if error_type == "UnexpectedError":
            return ErrorCode.WORKER_UNEXPECTED
        return ErrorCode.JM_DOWNLOAD_FAILED

    def _update_download_progress(self, job_id: str, job_dir: Path, idle_seconds: float | None = None) -> None:
        downloaded_files = self._count_downloaded_images(job_dir)
        if downloaded_files > 0:
            message = f"下载中，已保存 {downloaded_files} 张图片"
        else:
            message = "下载中，正在获取详情或等待图片写入"
        if idle_seconds is not None and idle_seconds >= 60:
            message = f"{message}，已 {int(idle_seconds // 60)} 分钟没有新文件写入"

        with self._connect() as conn:
            row = conn.execute(
                "SELECT album_id, total_files FROM jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            album_id = str(row["album_id"]) if row else "?"
            total_files = int(row["total_files"] or 0) if row else 0
            conn.execute(
                """
                UPDATE jobs
                SET downloaded_files = ?, progress_message = ?, updated_at = ?
                WHERE job_id = ? AND status = ?
                """,
                (downloaded_files, message, self._now(), job_id, JobStatus.DOWNLOADING.value),
            )

        self._render_console_progress(
            job_id,
            album_id,
            downloaded_files,
            total_files,
            "downloading",
            message,
            idle_seconds=idle_seconds,
        )

    def _render_console_progress_from_job(
        self,
        job_id: str,
        status: str,
        *,
        final: bool = False,
        error_code: str | None = None,
    ) -> None:
        job = self.get_job(job_id)
        if job is None:
            return
        self._render_console_progress(
            job_id,
            str(job.get("album_id") or "?"),
            int(job.get("downloaded_files") or 0),
            int(job.get("total_files") or 0),
            status,
            str(job.get("progress_message") or ""),
            final=final,
            error_code=error_code or job.get("error_code"),
        )

    @staticmethod
    def _render_console_progress(
        job_id: str,
        album_id: str,
        downloaded_files: int,
        total_files: int,
        status: str,
        message: str,
        *,
        idle_seconds: float | None = None,
        final: bool = False,
        error_code: str | None = None,
    ) -> None:
        width = 28
        display_downloaded = downloaded_files
        if status == "completed" and total_files > 0:
            display_downloaded = max(downloaded_files, total_files)

        if total_files > 0:
            ratio = min(max(display_downloaded / total_files, 0.0), 1.0)
            filled = min(width, int(round(width * ratio)))
            percent = f"{ratio * 100:5.1f}%"
            count = f"{display_downloaded}/{total_files}"
        else:
            filled = min(width, downloaded_files % (width + 1)) if downloaded_files else 0
            percent = "  ?.??%"
            count = f"{downloaded_files}/?"

        bar = "#" * filled + "-" * (width - filled)
        short_job_id = job_id.split("-", 1)[0]
        idle_text = ""
        if idle_seconds is not None and idle_seconds >= 60:
            idle_text = f" idle={int(idle_seconds)}s"
        error_text = f" error_code={error_code}" if error_code else ""
        line = (
            f"JM{album_id} {short_job_id} {status:<11} "
            f"[{bar}] {percent} {count} {message}{idle_text}{error_text}"
        )
        stream = sys.stderr
        if stream.isatty() and not final:
            stream.write("\r" + line + " " * 8)
        else:
            stream.write(line + "\n")
        stream.flush()

    def _count_downloaded_images(self, job_dir: Path) -> int:
        images_dir = job_dir / "images"
        if not images_dir.exists():
            return 0
        return sum(
            1
            for path in images_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in self.IMAGE_SUFFIXES and path.stat().st_size > 0
        )

    def _activity_snapshot(self, job_dir: Path) -> JobActivitySnapshot:
        if not job_dir.exists():
            return JobActivitySnapshot(file_count=0, total_size=0, latest_mtime_ns=0)

        file_count = 0
        total_size = 0
        latest_mtime_ns = 0
        for path in job_dir.rglob("*"):
            try:
                if not path.is_file():
                    continue
                stat = path.stat()
            except OSError:
                continue
            file_count += 1
            total_size += max(0, stat.st_size)
            latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
        return JobActivitySnapshot(
            file_count=file_count,
            total_size=total_size,
            latest_mtime_ns=latest_mtime_ns,
        )

    def _has_terminal_status(self, job_id: str) -> bool:
        job = self.get_job(job_id)
        return job is not None and job["status"] in {JobStatus.COMPLETED.value, JobStatus.FAILED.value}

    def _update_status(self, job_id: str, status: JobStatus, progress_message: str | None = None) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, progress_message = COALESCE(?, progress_message), updated_at = ?
                WHERE job_id = ?
                """,
                (status.value, progress_message, self._now(), job_id),
            )

    def _queued_job_ids(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT job_id FROM jobs WHERE status = ? ORDER BY created_at ASC",
                (JobStatus.QUEUED.value,),
            ).fetchall()
        return [row["job_id"] for row in rows]

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _ensure_column(conn: sqlite3.Connection, column_name: str, ddl: str) -> None:
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(jobs)").fetchall()
        }
        if column_name not in columns:
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {column_name} {ddl}")

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _format_duration(seconds: int) -> str:
        if seconds < 60:
            return f"{seconds} 秒"
        minutes = seconds // 60
        if seconds % 60 == 0:
            return f"{minutes} 分钟"
        return f"{minutes} 分钟 {seconds % 60} 秒"

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _path_mtime_before(path: Path, cutoff: datetime) -> bool:
        try:
            mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        except OSError:
            return False
        return mtime <= cutoff

    def _cleanup_children_older_than(self, directory: Path, cutoff: datetime) -> int:
        if not directory.exists():
            return 0
        removed = 0
        for path in directory.iterdir():
            if not self._path_mtime_before(path, cutoff):
                continue
            if path.is_dir():
                removed += 1 if self._remove_child_tree(path, directory) else 0
            elif self._remove_child_file(path, directory):
                removed += 1
        return removed

    @staticmethod
    def _remove_child_tree(path: Path, parent: Path) -> bool:
        resolved_path = path.resolve()
        resolved_parent = parent.resolve()
        if resolved_path == resolved_parent or not resolved_path.is_relative_to(resolved_parent):
            return False
        if not resolved_path.exists():
            return False
        try:
            shutil.rmtree(resolved_path)
        except OSError:
            return False
        return not resolved_path.exists()

    @staticmethod
    def _remove_child_file(path: Path, parent: Path) -> bool:
        resolved_path = path.resolve()
        resolved_parent = parent.resolve()
        if resolved_path == resolved_parent or not resolved_path.is_relative_to(resolved_parent):
            return False
        if not resolved_path.is_file():
            return False
        try:
            resolved_path.unlink()
        except OSError:
            return False
        return True
