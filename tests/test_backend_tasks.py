from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend import downloader
from backend.models import JobStatus
from backend.task_manager import JobManager, JobManagerConfig


@pytest.mark.asyncio
async def test_download_failure_marks_job_failed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_download(_album_id: str, _option_path: Path, _job_dir: Path) -> Path:
        raise downloader.DownloadError("下载失败，请稍后重试")

    monkeypatch.setattr(downloader, "download_album_pdf", fail_download)
    manager = JobManager(
        JobManagerConfig(
            data_dir=tmp_path / "data",
            option_path=tmp_path / "jmcomic-option.yml",
            max_concurrent_jobs=1,
            job_timeout_seconds=5,
        )
    )

    await manager.start()
    try:
        job = await manager.create_job("123456", "10001", "20001")
        await asyncio.wait_for(manager.join(), timeout=1)
        stored = manager.get_job(job["job_id"])
    finally:
        await manager.stop()

    assert stored is not None
    assert stored["status"] == JobStatus.FAILED.value
    assert stored["error_message"] == "下载失败，请稍后重试"


def test_pdf_not_generated_raises(tmp_path: Path) -> None:
    with pytest.raises(downloader.PdfGenerationError, match="未找到输出文件"):
        downloader._finalize_single_pdf("123456", tmp_path)


def test_update_download_progress_counts_images(tmp_path: Path) -> None:
    manager = JobManager(
        JobManagerConfig(
            data_dir=tmp_path / "data",
            option_path=tmp_path / "jmcomic-option.yml",
            max_concurrent_jobs=1,
            job_timeout_seconds=5,
        )
    )
    manager.initialize()
    job_id = "job-progress"
    now = manager._now()
    with manager._connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, album_id, group_id, user_id, status,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, "123456", "10001", "20001", JobStatus.DOWNLOADING.value, now, now),
        )

    images_dir = tmp_path / "data" / "jobs" / job_id / "images" / "chapter-1"
    images_dir.mkdir(parents=True)
    (images_dir / "001.jpg").write_bytes(b"image")
    (images_dir / "002.webp").write_bytes(b"image")
    (images_dir / "ignore.txt").write_text("not image", encoding="utf-8")

    manager._update_download_progress(job_id, tmp_path / "data" / "jobs" / job_id)

    stored = manager.get_job(job_id)
    assert stored is not None
    assert stored["downloaded_files"] == 2
    assert stored["progress_message"] == "下载中，已保存 2 张图片"
