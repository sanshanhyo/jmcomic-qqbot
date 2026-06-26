from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend import downloader
from backend.models import JobStatus
from backend.task_manager import DuplicateJobError, JobManager, JobManagerConfig


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
    assert stored["error_code"] == "JM_DOWNLOAD_FAILED"


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
        next_job = await manager.create_job("222222", "10001", "20002")
        await asyncio.wait_for(manager.join(), timeout=8)
        stuck_stored = manager.get_job(stuck["job_id"])
        next_stored = manager.get_job(next_job["job_id"])
    finally:
        await manager.stop()

    assert stuck_stored is not None
    assert stuck_stored["status"] == JobStatus.FAILED.value
    assert stuck_stored["error_message"] == "下载超时，请稍后重试"
    assert stuck_stored["error_code"] == "JOB_TIMEOUT"

    assert next_stored is not None
    assert next_stored["status"] == JobStatus.COMPLETED.value
    assert next_stored["filename"] == "[JM222222]ok.pdf"


@pytest.mark.asyncio
async def test_stalled_download_without_file_activity_is_killed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = JobManager(
        JobManagerConfig(
            data_dir=tmp_path / "data",
            option_path=tmp_path / "jmcomic-option.yml",
            max_concurrent_jobs=1,
            job_timeout_seconds=30,
            job_stall_timeout_seconds=1,
            progress_interval_seconds=0.1,
        )
    )

    def command(_album_id: str, job_dir: Path, _result_path: Path) -> list[str]:
        return [
            sys.executable,
            "-c",
            (
                "import pathlib, sys, time; "
                "images = pathlib.Path(sys.argv[1]) / 'images'; "
                "images.mkdir(parents=True, exist_ok=True); "
                "(images / '001.jpg').write_bytes(b'image'); "
                "time.sleep(30)"
            ),
            str(job_dir),
        ]

    monkeypatch.setattr(manager, "_download_worker_command", command)

    await manager.start()
    try:
        job = await manager.create_job("333333", "10001", "20001")
        await asyncio.wait_for(manager.join(), timeout=5)
        stored = manager.get_job(job["job_id"])
    finally:
        await manager.stop()

    assert stored is not None
    assert stored["status"] == JobStatus.FAILED.value
    assert "下载卡住" in stored["error_message"]
    assert stored["error_code"] == "JOB_STALLED"


@pytest.mark.asyncio
async def test_cancel_job_terminates_active_process(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    manager = JobManager(
        JobManagerConfig(
            data_dir=tmp_path / "data",
            option_path=tmp_path / "jmcomic-option.yml",
            max_concurrent_jobs=1,
            job_timeout_seconds=30,
            job_stall_timeout_seconds=0,
            progress_interval_seconds=0.1,
        )
    )

    def command(_album_id: str, _job_dir: Path, _result_path: Path) -> list[str]:
        return [sys.executable, "-c", "import time; time.sleep(30)"]

    monkeypatch.setattr(manager, "_download_worker_command", command)

    await manager.start()
    try:
        job = await manager.create_job("444444", "10001", "20001")
        await asyncio.sleep(0.2)
        cancelled = await manager.cancel_job(job["job_id"])
        await asyncio.wait_for(manager.join(), timeout=5)
        stored = manager.get_job(job["job_id"])
    finally:
        await manager.stop()

    assert cancelled is not None
    assert stored is not None
    assert stored["status"] == JobStatus.FAILED.value
    assert stored["error_message"] == "任务已取消"
    assert stored["error_code"] == "USER_CANCELLED"



@pytest.mark.asyncio
async def test_same_user_active_job_is_rejected(tmp_path: Path) -> None:
    manager = JobManager(
        JobManagerConfig(
            data_dir=tmp_path / "data",
            option_path=tmp_path / "jmcomic-option.yml",
            max_concurrent_jobs=1,
            job_timeout_seconds=5,
        )
    )
    manager.initialize()

    first = await manager.create_job("111111", "10001", "20001")

    with pytest.raises(DuplicateJobError) as exc_info:
        await manager.create_job("222222", "10001", "20001")

    other_user = await manager.create_job("333333", "10001", "20002")

    assert exc_info.value.existing_job["job_id"] == first["job_id"]
    assert other_user["album_id"] == "333333"


@pytest.mark.asyncio
async def test_cancel_active_job_for_user_marks_failed(tmp_path: Path) -> None:
    manager = JobManager(
        JobManagerConfig(
            data_dir=tmp_path / "data",
            option_path=tmp_path / "jmcomic-option.yml",
            max_concurrent_jobs=1,
            job_timeout_seconds=5,
        )
    )
    manager.initialize()

    job = await manager.create_job("111111", "10001", "20001")
    cancelled = await manager.cancel_active_job_for_user("10001", "20001")
    stored = manager.get_job(job["job_id"])

    assert cancelled is not None
    assert stored is not None
    assert stored["status"] == JobStatus.FAILED.value
    assert stored["error_message"] == "任务已取消"
    assert stored["error_code"] == "USER_CANCELLED"

    assert manager.find_active_job_for_user("10001", "20001") is None


@pytest.mark.asyncio
async def test_cache_cleanup_removes_old_terminal_files_only(tmp_path: Path) -> None:
    manager = JobManager(
        JobManagerConfig(
            data_dir=tmp_path / "data",
            option_path=tmp_path / "jmcomic-option.yml",
            max_concurrent_jobs=1,
            cache_cleanup_interval_seconds=0,
            job_cache_ttl_seconds=1,
            bot_download_cache_ttl_seconds=1,
            preview_cache_ttl_seconds=1,
        )
    )
    manager.initialize()

    old_time = "2000-01-01T00:00:00+00:00"
    completed_job_id = "old-completed-job"
    active_job_id = "old-active-job"
    with manager._connect() as conn:
        conn.executemany(
            """
            INSERT INTO jobs (
                job_id, album_id, group_id, user_id, status,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    completed_job_id,
                    "111111",
                    "10001",
                    "20001",
                    JobStatus.COMPLETED.value,
                    old_time,
                    old_time,
                ),
                (
                    active_job_id,
                    "222222",
                    "10001",
                    "20002",
                    JobStatus.DOWNLOADING.value,
                    old_time,
                    old_time,
                ),
            ],
        )

    completed_dir = manager.jobs_dir / completed_job_id
    active_dir = manager.jobs_dir / active_job_id
    completed_dir.mkdir(parents=True)
    active_dir.mkdir(parents=True)

    bot_cache_dir = manager.bot_downloads_dir / "old-upload"
    bot_cache_dir.mkdir(parents=True)
    (bot_cache_dir / "old.pdf").write_bytes(b"%PDF-1.4\n")
    preview_file = manager.previews_dir / "old-preview.json"
    preview_file.parent.mkdir(parents=True)
    preview_file.write_text("{}", encoding="utf-8")
    for path in [bot_cache_dir / "old.pdf", bot_cache_dir, preview_file]:
        os.utime(path, (0, 0))

    stats = await manager.cleanup_cache_once()

    assert stats == {"job_dirs": 1, "bot_downloads": 1, "previews": 1}
    assert not completed_dir.exists()
    assert active_dir.exists()
    assert not bot_cache_dir.exists()
    assert not preview_file.exists()


def test_pdf_not_generated_raises(tmp_path: Path) -> None:
    with pytest.raises(downloader.PdfGenerationError, match="未找到输出文件"):
        downloader._finalize_single_pdf("123456", tmp_path)


def test_pdf_renamed_with_album_title(tmp_path: Path) -> None:
    output_dir = tmp_path / "pdf"
    output_dir.mkdir()
    (output_dir / "123456_Original Name.pdf").write_bytes(b"%PDF-1.4\n")

    pdf_path = downloader._finalize_single_pdf(
        "123456",
        output_dir,
        preferred_title='A/B: "Title"?',
    )

    assert pdf_path.name == "[JM123456]A_B_ _Title__.pdf"
    assert pdf_path.read_bytes() == b"%PDF-1.4\n"


def test_fallback_pdf_generated_from_downloaded_images(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    class FakeImg2Pdf:
        converted_paths: list[str] = []

        @classmethod
        def convert(cls, image_paths: list[str]) -> bytes:
            cls.converted_paths = image_paths
            return b"%PDF-1.4\n"

    images_dir = tmp_path / "images" / "chapter"
    output_dir = tmp_path / "pdf"
    images_dir.mkdir(parents=True)
    (images_dir / "10.jpg").write_bytes(b"image")
    (images_dir / "2.jpg").write_bytes(b"image")

    monkeypatch.setattr(downloader, "_load_img2pdf_module", lambda: FakeImg2Pdf)

    pdf_path = downloader._finalize_or_convert_pdf(
        "123456",
        output_dir,
        tmp_path / "images",
        title="A Test Title",
    )

    assert pdf_path.name == "[JM123456]A Test Title.pdf"
    assert pdf_path.read_bytes() == b"%PDF-1.4\n"
    assert [Path(path).name for path in FakeImg2Pdf.converted_paths] == ["2.jpg", "10.jpg"]


def test_download_threading_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    option = SimpleNamespace(
        download=SimpleNamespace(
            threading=SimpleNamespace(image=20, photo=4),
        ),
    )

    monkeypatch.setenv("JM_DOWNLOAD_IMAGE_THREADS", "40")
    monkeypatch.setenv("JM_DOWNLOAD_PHOTO_THREADS", "8")

    downloader._set_download_threading(option)

    assert option.download.threading.image == 16
    assert option.download.threading.photo == 4


def test_download_threading_cap_can_be_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    option = SimpleNamespace(
        download=SimpleNamespace(
            threading=SimpleNamespace(image=20, photo=4),
        ),
    )

    monkeypatch.setenv("JM_DOWNLOAD_IMAGE_THREADS", "40")
    monkeypatch.setenv("JM_DOWNLOAD_PHOTO_THREADS", "8")
    monkeypatch.setenv("JM_DOWNLOAD_MAX_IMAGE_THREADS", "40")
    monkeypatch.setenv("JM_DOWNLOAD_MAX_PHOTO_THREADS", "8")

    downloader._set_download_threading(option)

    assert option.download.threading.image == 40
    assert option.download.threading.photo == 8


def test_preview_page_count_falls_back_to_photo_details() -> None:
    class FakeClient:
        def __init__(self) -> None:
            self.requested: list[str] = []

        def get_photo_detail(self, photo_id: str, fetch_album: bool = False) -> SimpleNamespace:
            self.requested.append(photo_id)
            pages = {
                "p1": ["1.jpg"] * 80,
                "p2": ["1.jpg"] * 30,
                "p3": ["1.jpg"] * 20,
            }[photo_id]
            return SimpleNamespace(page_arr=pages)

    album = SimpleNamespace(
        page_count=0,
        episode_list=[
            ("p1", "1", "chapter 1"),
            ("p2", "2", "chapter 2"),
            ("p3", "3", "chapter 3"),
        ],
    )
    client = FakeClient()

    page_count, is_estimated = downloader._resolve_preview_page_count(client, album, stop_after=101)

    assert page_count == 110
    assert is_estimated is True
    assert client.requested == ["p1", "p2"]


def test_console_progress_bar_with_total(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    manager = JobManager(
        JobManagerConfig(
            data_dir=tmp_path / "data",
            option_path=tmp_path / "jmcomic-option.yml",
            max_concurrent_jobs=1,
            job_timeout_seconds=5,
        )
    )
    manager.initialize()
    job_id = "job-progress-bar"
    now = manager._now()
    with manager._connect() as conn:
        conn.execute(
            """
            INSERT INTO jobs (
                job_id, album_id, group_id, user_id, status,
                total_files, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (job_id, "123456", "10001", "20001", JobStatus.DOWNLOADING.value, 4, now, now),
        )

    images_dir = tmp_path / "data" / "jobs" / job_id / "images"
    images_dir.mkdir(parents=True)
    (images_dir / "001.jpg").write_bytes(b"image")
    (images_dir / "002.jpg").write_bytes(b"image")

    manager._update_download_progress(job_id, tmp_path / "data" / "jobs" / job_id)

    output = capsys.readouterr().err
    assert "JM123456" in output
    assert "50.0%" in output
    assert "2/4" in output


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
