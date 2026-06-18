from __future__ import annotations

import asyncio
import sys
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
    manager = JobManager(
        JobManagerConfig(
            data_dir=tmp_path / "data",
            option_path=tmp_path / "jmcomic-option.yml",
            max_concurrent_jobs=1,
            job_timeout_seconds=5,
        )
    )

    def fail_command(_album_id: str, _job_dir: Path, result_path: Path) -> list[str]:
        return [
            sys.executable,
            "-c",
            (
                "import json, pathlib, sys; "
                "pathlib.Path(sys.argv[1]).write_text("
                "json.dumps({'ok': False, 'user_message': '下载失败，请稍后重试'}, ensure_ascii=False), "
                "encoding='utf-8'"
                "); raise SystemExit(2)"
            ),
            str(result_path),
        ]

    monkeypatch.setattr(manager, "_download_worker_command", fail_command)

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


@pytest.mark.asyncio
async def test_stuck_download_times_out_and_worker_continues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = JobManager(
        JobManagerConfig(
            data_dir=tmp_path / "data",
            option_path=tmp_path / "jmcomic-option.yml",
            max_concurrent_jobs=1,
            job_timeout_seconds=1,
        )
    )

    def command(album_id: str, job_dir: Path, result_path: Path) -> list[str]:
        if album_id == "111111":
            return [sys.executable, "-c", "import time; time.sleep(30)"]

        return [
            sys.executable,
            "-c",
            (
                "import json, pathlib, sys; "
                "job_dir = pathlib.Path(sys.argv[1]); "
                "result_path = pathlib.Path(sys.argv[2]); "
                "album_id = sys.argv[3]; "
                "pdf = job_dir / 'pdf' / f'[JM{album_id}]ok.pdf'; "
                "pdf.parent.mkdir(parents=True, exist_ok=True); "
                "pdf.write_bytes(b'%PDF-1.4\\n'); "
                "result_path.write_text(json.dumps({'ok': True, 'pdf_path': str(pdf)}), encoding='utf-8')"
            ),
            str(job_dir),
            str(result_path),
            album_id,
        ]

    monkeypatch.setattr(manager, "_download_worker_command", command)

    await manager.start()
    try:
        stuck = await manager.create_job("111111", "10001", "20001")
        next_job = await manager.create_job("222222", "10001", "20001")
        await asyncio.wait_for(manager.join(), timeout=8)
        stuck_stored = manager.get_job(stuck["job_id"])
        next_stored = manager.get_job(next_job["job_id"])
    finally:
        await manager.stop()

    assert stuck_stored is not None
    assert stuck_stored["status"] == JobStatus.FAILED.value
    assert stuck_stored["error_message"] == "下载超时，请稍后重试"

    assert next_stored is not None
    assert next_stored["status"] == JobStatus.COMPLETED.value
    assert next_stored["filename"] == "[JM222222]ok.pdf"


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
