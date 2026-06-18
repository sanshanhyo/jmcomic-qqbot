from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import JobStatus

logger = logging.getLogger(__name__)


class DuplicateJobError(Exception):
    def __init__(self, existing_job: dict[str, Any]) -> None:
        super().__init__("duplicate active job")
        self.existing_job = existing_job


class DownloadWorkerError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


@dataclass(frozen=True)
class JobManagerConfig:
    data_dir: Path
    option_path: Path
    max_concurrent_jobs: int = 1
    job_timeout_seconds: int = 1800


class JobManager:
    IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

    def __init__(self, config: JobManagerConfig) -> None:
        self.config = config
        self.data_dir = config.data_dir.resolve()
        self.jobs_dir = self.data_dir / "jobs"
        self.db_path = self.data_dir / "jobs.sqlite3"
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []

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
                    downloaded_files INTEGER NOT NULL DEFAULT 0,
                    progress_message TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            self._ensure_column(conn, "downloaded_files", "INTEGER NOT NULL DEFAULT 0")
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

    async def stop(self) -> None:
        for worker in self._workers:
            worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def join(self) -> None:
        await self._queue.join()

    async def create_job(self, album_id: str, group_id: str, user_id: str) -> dict[str, Any]:
        existing = self.find_active_job(album_id, group_id)
        if existing is not None:
            raise DuplicateJobError(existing)

        now = self._now()
        job_id = str(uuid.uuid4())
        try:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO jobs (
                        job_id, album_id, group_id, user_id, status,
                        filename, file_path, error_message, downloaded_files,
                        progress_message, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, 0, ?, ?, ?)
                    """,
                    (
                        job_id,
                        album_id,
                        group_id,
                        user_id,
                        JobStatus.QUEUED.value,
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

        await self._queue.put(job_id)
        created = self.get_job(job_id)
        if created is None:
            raise RuntimeError("created job disappeared")
        return created

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT job_id, album_id, group_id, user_id, status,
                       filename, file_path, error_message, downloaded_files,
                       progress_message, created_at, updated_at
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

    def find_active_job(self, album_id: str, group_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT job_id, album_id, group_id, user_id, status,
                       filename, file_path, error_message, downloaded_files,
                       progress_message, created_at, updated_at
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
                self._mark_failed(job_id, "任务执行失败，请查看服务日志")
            finally:
                self._queue.task_done()

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
            )
            self._update_status(job_id, JobStatus.CONVERTING, "图片下载完成，正在生成 PDF")
            self._mark_completed(job_id, pdf_path)
        except asyncio.TimeoutError:
            logger.exception("Job %s timed out.", job_id)
            self._mark_failed(job_id, "下载超时，请稍后重试")
        except DownloadWorkerError as exc:
            logger.warning("Job %s failed in download worker: %s", job_id, exc.user_message)
            self._mark_failed(job_id, exc.user_message)
        except Exception:
            logger.exception("Job %s failed unexpectedly.", job_id)
            self._mark_failed(job_id, "下载或转换失败，请查看服务日志")

    def _mark_completed(self, job_id: str, pdf_path: Path) -> None:
        pdf_path = pdf_path.resolve()
        if not pdf_path.is_file() or pdf_path.stat().st_size <= 0:
            self._mark_failed(job_id, "PDF 生成失败：最终文件无效")
            return
        if not pdf_path.is_relative_to(self.jobs_dir.resolve()):
            self._mark_failed(job_id, "PDF 生成失败：输出路径异常")
            return

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, filename = ?, file_path = ?, error_message = NULL, updated_at = ?
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

    def _mark_failed(self, job_id: str, error_message: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, error_message = ?, updated_at = ?
                    , progress_message = ?
                WHERE job_id = ?
                """,
                (JobStatus.FAILED.value, error_message, self._now(), error_message, job_id),
            )

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
        deadline = asyncio.get_running_loop().time() + self.config.job_timeout_seconds

        try:
            while process.returncode is None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    await self._terminate_download_process(process)
                    raise asyncio.TimeoutError

                try:
                    await asyncio.wait_for(
                        process.wait(),
                        timeout=min(progress_interval_seconds, remaining),
                    )
                except asyncio.TimeoutError:
                    self._update_download_progress(job_id, job_dir)
                    continue

            self._update_download_progress(job_id, job_dir)
            return self._read_download_result(result_path, process.returncode)
        except asyncio.CancelledError:
            await self._terminate_download_process(process)
            raise

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
            raise DownloadWorkerError(f"下载进程异常退出，退出码：{returncode}")

        try:
            result = json.loads(result_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise DownloadWorkerError("下载进程结果文件无效") from exc

        if not result.get("ok"):
            raise DownloadWorkerError(result.get("user_message") or "下载失败，请稍后重试")

        pdf_path = result.get("pdf_path")
        if not isinstance(pdf_path, str) or not pdf_path:
            raise DownloadWorkerError("PDF 生成失败：结果路径无效")
        return Path(pdf_path).resolve()

    def _update_download_progress(self, job_id: str, job_dir: Path) -> None:
        downloaded_files = self._count_downloaded_images(job_dir)
        if downloaded_files > 0:
            message = f"下载中，已保存 {downloaded_files} 张图片"
        else:
            message = "下载中，正在获取详情或等待图片写入"

        with self._connect() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET downloaded_files = ?, progress_message = ?, updated_at = ?
                WHERE job_id = ? AND status = ?
                """,
                (downloaded_files, message, self._now(), job_id, JobStatus.DOWNLOADING.value),
            )

    def _count_downloaded_images(self, job_dir: Path) -> int:
        images_dir = job_dir / "images"
        if not images_dir.exists():
            return 0
        return sum(
            1
            for path in images_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in self.IMAGE_SUFFIXES and path.stat().st_size > 0
        )

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
