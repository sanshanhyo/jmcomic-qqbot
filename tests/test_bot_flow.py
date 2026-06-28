from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from typing import Awaitable

import pytest

import bot.main as bot_main
from bot.main import BotSettings, BotState, _download_and_upload, handle_group_message, monitor_job
from bot.napcat_client import NapCatAPIError


class FakeNapCat:
    def __init__(self, upload_failures: int = 0, image_failures: int = 0) -> None:
        self.sent: list[tuple[str, str]] = []
        self.uploads: list[tuple[str, Path, str]] = []
        self.upload_attempts = 0
        self.upload_failures = upload_failures
        self.image_attempts = 0
        self.image_failures = image_failures

    async def send_group_msg(self, group_id: str, message: str | list[dict]) -> dict:
        self.sent.append((group_id, message))
        return {"status": "ok", "retcode": 0}

    async def send_group_image(self, group_id: str, image_url: str) -> dict:
        self.image_attempts += 1
        if self.image_attempts <= self.image_failures:
            raise NapCatAPIError("image failed")
        self.sent.append((group_id, f"IMAGE:{image_url}"))
        return {"status": "ok", "retcode": 0}

    async def upload_group_file(self, group_id: str, file_path: str | Path, name: str) -> dict:
        self.upload_attempts += 1
        if self.upload_attempts <= self.upload_failures:
            raise NapCatAPIError("upload failed")
        self.uploads.append((group_id, Path(file_path), name))
        return {"status": "ok", "retcode": 0}


class FakeCreateBackend:
    def __init__(self, page_count: int | None = 80) -> None:
        self.created: list[tuple[str, str, str, int | None]] = []
        self.previewed: list[str] = []
        self.searches: list[tuple[str, int, int]] = []
        self.cancelled: list[str] = []
        self.admin_cancellations: list[str] = []
        self.active_queries: list[tuple[str, str]] = []
        self.cancelled_active: list[tuple[str, str]] = []
        self.active_job: dict | None = None
        self.page_count = page_count

    async def get_active_job(self, group_id: str, user_id: str) -> dict | None:
        self.active_queries.append((group_id, user_id))
        return self.active_job

    async def cancel_active_job(self, group_id: str, user_id: str) -> dict | None:
        self.cancelled_active.append((group_id, user_id))
        return self.active_job

    async def get_album_preview(self, album_id: str) -> dict:
        self.previewed.append(album_id)
        return {
            "album_id": album_id,
            "title": "A Test Album",
            "cover_url": "https://example.test/cover.jpg",
            "page_count": self.page_count,
            "estimated_seconds": 300,
            "estimated_text": "预计约 5-8 分钟",
        }

    async def search_albums(self, query: str, page: int = 1, limit: int = 5) -> dict:
        self.searches.append((query, page, limit))
        return {
            "query": query,
            "page": page,
            "total": 2,
            "results": [
                {"album_id": "111111", "title": "First Search Hit", "tags": ["tag1"]},
                {"album_id": "222222", "title": "Second Search Hit", "tags": ["tag2"]},
            ],
        }

    async def create_job(
        self,
        album_id: str,
        group_id: str,
        user_id: str,
        page_count: int | None = None,
    ) -> dict:
        self.created.append((album_id, group_id, user_id, page_count))
        return {"job_id": "job-123", "status": "queued"}

    async def cancel_job(self, job_id: str) -> dict:
        self.cancelled.append(job_id)
        return {"job_id": job_id, "status": "failed"}

    async def get_admin_status(self) -> dict:
        return {
            "cpu_percent": 12.5,
            "memory": {"used": 512 * 1024 * 1024, "total": 2 * 1024 * 1024 * 1024},
            "disk": {
                "used": 10 * 1024 * 1024 * 1024,
                "total": 40 * 1024 * 1024 * 1024,
                "free": 30 * 1024 * 1024 * 1024,
            },
            "cache": {"data": 1000, "jobs": 200, "bot_downloads": 300},
            "network": {"tx_bytes_per_second": 1024, "rx_bytes_per_second": 2048},
            "jobs": {"downloading": 1, "queued": 2, "converting": 0},
        }

    async def get_admin_queue(self, limit: int = 20) -> dict:
        return {
            "jobs": [
                {
                    "job_id": "abcdef1234567890",
                    "album_id": "123456",
                    "group_id": "10001",
                    "user_id": "20001",
                    "status": "downloading",
                    "downloaded_files": 50,
                    "total_files": 100,
                }
            ]
        }

    async def get_group_history(self, group_id: str, limit: int = 10) -> dict:
        return {
            "jobs": [
                {
                    "job_id": "group-history-job",
                    "album_id": "333333",
                    "group_id": group_id,
                    "user_id": "20002",
                    "status": "completed",
                    "updated_at": "2026-06-27T12:00:00+00:00",
                }
            ]
        }

    async def get_user_history(self, group_id: str, user_id: str, limit: int = 5) -> dict:
        return {
            "jobs": [
                {
                    "job_id": "user-history-job",
                    "album_id": "123456",
                    "group_id": group_id,
                    "user_id": user_id,
                    "status": "completed",
                    "updated_at": "2026-06-27T12:00:00+00:00",
                },
                {
                    "job_id": "failed-history-job",
                    "album_id": "222222",
                    "group_id": group_id,
                    "user_id": user_id,
                    "status": "failed",
                    "error_code": "JOB_TIMEOUT",
                    "updated_at": "2026-06-27T12:10:00+00:00",
                },
            ]
        }

    async def cleanup_cache(self) -> dict:
        return {"freed_bytes": 2048, "stats": {"job_dirs": 1, "bot_downloads": 2, "previews": 3}}

    async def admin_cancel_job(self, target: str) -> dict:
        self.admin_cancellations.append(target)
        return {"job": {"job_id": "abcdef1234567890", "album_id": "123456", "status": "failed"}}


class FakeDownloadBackend:
    async def download_file(self, job_id: str, dest_path: str | Path) -> Path:
        path = Path(dest_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"%PDF-1.4\n")
        return path


class FakeFailedJobBackend:
    async def get_job(self, _job_id: str) -> dict:
        return {
            "job_id": "job-123",
            "status": "failed",
            "error_message": "下载失败，请稍后重试",
            "error_code": "JM_DOWNLOAD_FAILED",
        }


class TaskCollector:
    def __init__(self) -> None:
        self.count = 0

    def __call__(self, awaitable: Awaitable[None]) -> None:
        self.count += 1
        awaitable.close()


def _settings(tmp_path: Path, enable_search: bool = True) -> BotSettings:
    return BotSettings(
        bot_qq_id="12345",
        napcat_ws_url="ws://127.0.0.1:3001",
        napcat_http_url="http://127.0.0.1:3000",
        napcat_access_token=None,
        backend_url="http://127.0.0.1:8000",
        backend_api_token=None,
        data_dir=tmp_path,
        job_timeout_seconds=30,
        poll_interval_seconds=0.01,
        enable_search=enable_search,
        search_confirm_timeout_seconds=60,
    )


def _group_event(message: list[dict], user_id: str = "20001", role: str = "member") -> dict:
    return {
        "message_type": "group",
        "group_id": "10001",
        "user_id": user_id,
        "sender": {"role": role},
        "message": message,
    }


def test_split_pdf_for_upload_creates_valid_parts(tmp_path: Path) -> None:
    import img2pdf
    import pikepdf
    from PIL import Image

    image_paths: list[Path] = []
    for index in range(3):
        image_path = tmp_path / f"{index}.jpg"
        Image.new("RGB", (32, 32), "white").save(image_path)
        image_paths.append(image_path)

    pdf_path = tmp_path / "album.pdf"
    pdf_path.write_bytes(img2pdf.convert([str(path) for path in image_paths]))

    max_upload_bytes = 2000
    parts = bot_main._split_pdf_for_upload(pdf_path, "album.pdf", max_upload_bytes=max_upload_bytes)

    assert len(parts) == 3
    assert [name for _path, name in parts] == [
        "part01-of03_album.pdf",
        "part02-of03_album.pdf",
        "part03-of03_album.pdf",
    ]
    for part_path, _part_name in parts:
        assert part_path.stat().st_size <= max_upload_bytes
        with pikepdf.Pdf.open(part_path) as part_pdf:
            assert len(part_pdf.pages) == 1


def test_part_filename_is_truncated_by_utf8_bytes() -> None:
    filename = "[JM434803]" + ("譚雅奉旨生子之事" * 30) + ".pdf"

    part_name = bot_main._part_filename(filename, 1, 3)

    assert part_name == "JM434803_part01-of03.pdf"
    assert len(part_name.encode("utf-8")) <= bot_main.MAX_FILENAME_BYTES


@pytest.mark.asyncio
async def test_handle_group_message_sends_usage_without_number(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " hello"}},
            ]
        ),
        _settings(tmp_path),
        BotState(pending_downloads={}),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert napcat.sent == [("10001", "用法：@我 JM123456")]
    assert backend.created == []


@pytest.mark.asyncio
async def test_empty_at_sends_home_message(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()
    settings = replace(
        _settings(tmp_path),
        bot_display_name="测试机器人",
        manager_name="散山肆水HyO",
        manager_qq="2456014618",
    )

    await handle_group_message(
        _group_event([{"type": "at", "data": {"qq": "12345"}}]),
        settings,
        BotState(),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert "这里是「测试机器人」" in napcat.sent[-1][1]
    assert "散山肆水HyO（QQ：2456014618）" in napcat.sent[-1][1]
    assert "https://github.com/sanshanhyo/SanBot" in napcat.sent[-1][1]


@pytest.mark.asyncio
async def test_help_and_features_commands(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()
    state = BotState()

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 帮助"}},
            ]
        ),
        _settings(tmp_path),
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )
    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 功能"}},
            ]
        ),
        _settings(tmp_path),
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert "使用说明" in napcat.sent[-2][1]
    assert "当前功能" in napcat.sent[-1][1]


@pytest.mark.asyncio
async def test_user_history_command(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 我的任务"}},
            ]
        ),
        _settings(tmp_path),
        BotState(),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert "你的最近任务" in napcat.sent[-1][1]
    assert "JM123456 已完成" in napcat.sent[-1][1]
    assert "JM222222 错误：JOB_TIMEOUT" in napcat.sent[-1][1]


@pytest.mark.asyncio
async def test_group_history_requires_admin(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 最近任务"}},
            ]
        ),
        _settings(tmp_path),
        BotState(),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert napcat.sent[-1] == ("10001", "这个命令需要群主、群管理员或机器人管理者执行。")


@pytest.mark.asyncio
async def test_group_admin_can_query_group_history(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 最近任务"}},
            ],
            role="admin",
        ),
        _settings(tmp_path),
        BotState(),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert "本群最近任务" in napcat.sent[-1][1]
    assert "JM333333 已完成" in napcat.sent[-1][1]
    assert "用户：20002" in napcat.sent[-1][1]


@pytest.mark.asyncio
async def test_handle_group_message_sends_preview_without_creating_job(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()
    tasks = TaskCollector()
    state = BotState(pending_downloads={})

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " jm123456"}},
            ]
        ),
        _settings(tmp_path),
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        tasks,
    )

    assert backend.previewed == ["123456"]
    assert backend.created == []
    assert tasks.count == 0
    assert napcat.sent[0] == ("10001", "我已经接收到 JM123456，正在用全力获取信息中(绝对没有偷懒！)...")
    assert "标题是A Test Album" in napcat.sent[1][1]
    assert ("10001", "20001") in state.pending_downloads
    await asyncio.sleep(0)
    assert napcat.sent[2] == ("10001", "IMAGE:https://example.test/cover.jpg")


@pytest.mark.asyncio
async def test_cover_url_failure_sends_cached_cover(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def fake_download_cover_image(cover_url: str, cache_dir: Path, album_id: str) -> Path:
        assert cover_url == "https://example.test/cover.jpg"
        cover_path = cache_dir / f"JM{album_id}.jpg"
        cover_path.parent.mkdir(parents=True, exist_ok=True)
        cover_path.write_bytes(b"fake image")
        return cover_path.resolve()

    monkeypatch.setattr(bot_main, "_download_cover_image", fake_download_cover_image)
    napcat = FakeNapCat(image_failures=1)
    backend = FakeCreateBackend()
    state = BotState(pending_downloads={})

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " JM123456"}},
            ]
        ),
        _settings(tmp_path),
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    for _ in range(5):
        await asyncio.sleep(0)

    assert napcat.image_attempts == 2
    expected_cover_path = tmp_path.resolve() / "cover_cache" / "JM123456.jpg"
    assert "标题是A Test Album" in napcat.sent[1][1]
    assert napcat.sent[2] == ("10001", f"IMAGE:{expected_cover_path}")


@pytest.mark.asyncio
async def test_slow_cover_send_does_not_block_preview(tmp_path: Path) -> None:
    class SlowImageNapCat(FakeNapCat):
        def __init__(self) -> None:
            super().__init__()
            self.release_image = asyncio.Event()

        async def send_group_image(self, group_id: str, image_url: str) -> dict:
            self.image_attempts += 1
            await self.release_image.wait()
            self.sent.append((group_id, f"IMAGE:{image_url}"))
            return {"status": "ok", "retcode": 0}

    napcat = SlowImageNapCat()
    backend = FakeCreateBackend()
    state = BotState(pending_downloads={})

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " JM123456"}},
            ]
        ),
        _settings(tmp_path),
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )
    await asyncio.sleep(0)

    assert napcat.image_attempts == 1
    assert "标题是A Test Album" in napcat.sent[1][1]
    assert len(napcat.sent) == 2

    napcat.release_image.set()
    await asyncio.sleep(0)
    assert napcat.sent[-1] == ("10001", "IMAGE:https://example.test/cover.jpg")


@pytest.mark.asyncio
async def test_search_command_can_be_disabled_by_config(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()
    state = BotState(pending_downloads={})

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 搜索 戦乙女"}},
            ]
        ),
        _settings(tmp_path, enable_search=False),
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert backend.searches == []
    assert napcat.sent[-1] == ("10001", "搜索功能还没有开启，稍后再来找我吧。")
    assert state.pending_searches == {}


@pytest.mark.asyncio
async def test_search_result_selection_sends_album_preview(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()
    tasks = TaskCollector()
    state = BotState(pending_downloads={})
    settings = _settings(tmp_path, enable_search=True)

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 搜索 戦乙女"}},
            ]
        ),
        settings,
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        tasks,
    )

    assert backend.searches == [("戦乙女", 1, 5)]
    assert ("10001", "20001") in state.pending_searches
    assert napcat.sent[-1] == (
        "10001",
        "搜索结果：戦乙女\n1. JM111111 First Search Hit\n2. JM222222 Second Search Hit\n回复 1-2 选择，回复“取消”放弃。",
    )

    await handle_group_message(
        _group_event([{"type": "text", "data": {"text": "1"}}]),
        settings,
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        tasks,
    )

    assert backend.previewed == ["111111"]
    assert state.pending_searches == {}
    assert ("10001", "20001") in state.pending_downloads
    assert napcat.sent[-2] == ("10001", "已选择 JM111111，我先获取封面和页数给你确认。")
    assert "标题是A Test Album" in napcat.sent[-1][1]
    assert tasks.count == 0
    await asyncio.sleep(0)
    assert napcat.sent[-1] == ("10001", "IMAGE:https://example.test/cover.jpg")


@pytest.mark.asyncio
async def test_confirm_download_creates_job(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()
    tasks = TaskCollector()
    state = BotState(pending_downloads={})
    settings = _settings(tmp_path)

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " JM123456"}},
            ]
        ),
        settings,
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        tasks,
    )

    await handle_group_message(
        _group_event([{"type": "text", "data": {"text": "下载"}}]),
        settings,
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        tasks,
    )

    assert backend.created == [("123456", "10001", "20001", 80)]
    assert napcat.sent[-1] == (
        "10001",
        "我已经接收到 JM123456 啦，任务编号是 job-123\n我预计时间是 预计约 5-8 分钟，请你稍等片刻啦",
    )
    assert tasks.count == 1
    assert state.pending_downloads == {}


@pytest.mark.asyncio
async def test_large_album_requires_second_confirmation(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend(page_count=120)
    tasks = TaskCollector()
    state = BotState(pending_downloads={})
    settings = _settings(tmp_path)

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " JM123456"}},
            ]
        ),
        settings,
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        tasks,
    )

    await handle_group_message(
        _group_event([{"type": "text", "data": {"text": "下载"}}]),
        settings,
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        tasks,
    )

    assert backend.created == []
    assert tasks.count == 0
    assert "超过 100 页" in napcat.sent[-1][1]
    assert state.pending_downloads[("10001", "20001")].large_warning_sent is True

    await handle_group_message(
        _group_event([{"type": "text", "data": {"text": "下载"}}]),
        settings,
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        tasks,
    )

    assert backend.created == [("123456", "10001", "20001", 120)]
    assert tasks.count == 1
    assert state.pending_downloads == {}


@pytest.mark.asyncio
async def test_new_jm_is_rejected_when_user_has_active_download(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()
    backend.active_job = {"job_id": "job-123", "album_id": "123456", "status": "downloading"}
    state = BotState()

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " JM654321"}},
            ]
        ),
        _settings(tmp_path),
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert backend.previewed == []
    assert napcat.sent[-1] == (
        "10001",
        "JM123456 已经正在下载或排队中啦！回复“取消下载”可以停止当前任务。",
    )


@pytest.mark.asyncio
async def test_active_download_can_be_cancelled(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()
    backend.active_job = {"job_id": "job-123", "album_id": "123456", "status": "downloading"}
    state = BotState()

    await handle_group_message(
        _group_event([{"type": "text", "data": {"text": "取消下载"}}]),
        _settings(tmp_path),
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert backend.cancelled_active == [("10001", "20001")]
    assert napcat.sent[-1] == ("10001", "已取消 JM123456 任务。")


@pytest.mark.asyncio
async def test_manager_can_query_admin_status(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()
    settings = replace(_settings(tmp_path), manager_qq_ids={"2456014618"})

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 状态"}},
            ],
            user_id="2456014618",
            role="member",
        ),
        settings,
        BotState(),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert "服务器状态" in napcat.sent[-1][1]
    assert "CPU：12.5%" in napcat.sent[-1][1]


@pytest.mark.asyncio
async def test_group_admin_can_query_queue(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 队列"}},
            ],
            role="admin",
        ),
        _settings(tmp_path),
        BotState(),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert "当前队列" in napcat.sent[-1][1]
    assert "JM123456 下载中（50%）" in napcat.sent[-1][1]


@pytest.mark.asyncio
async def test_member_cannot_run_admin_status(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 状态"}},
            ]
        ),
        _settings(tmp_path),
        BotState(),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert napcat.sent[-1] == ("10001", "这个命令需要群主、群管理员或机器人管理者执行。")


@pytest.mark.asyncio
async def test_cleanup_cache_requires_manager(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 清理缓存"}},
            ],
            role="owner",
        ),
        _settings(tmp_path),
        BotState(),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert napcat.sent[-1] == ("10001", "清理缓存属于维护操作，只允许机器人管理者执行。")


@pytest.mark.asyncio
async def test_manager_can_cleanup_cache(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()
    settings = replace(_settings(tmp_path), manager_qq_ids={"2456014618"})

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 清理缓存"}},
            ],
            user_id="2456014618",
        ),
        settings,
        BotState(),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert "缓存清理完成，释放 2.0KB" in napcat.sent[-1][1]


@pytest.mark.asyncio
async def test_cooldown_blocks_repeated_new_command(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()
    settings = replace(_settings(tmp_path), user_command_cooldown_seconds=60)
    state = BotState()

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " JM123456"}},
            ]
        ),
        settings,
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )
    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " JM654321"}},
            ]
        ),
        settings,
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert backend.previewed == ["123456"]
    assert napcat.sent[-1] == ("10001", "别急别急，60 秒后再发新任务或搜索吧。")


@pytest.mark.asyncio
async def test_admin_cancel_uploading_job_marks_cancelled(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeCreateBackend()
    settings = replace(_settings(tmp_path), manager_qq_ids={"2456014618"})
    state = BotState(
        uploading_jobs={
            "abcdef1234567890": bot_main.UploadingJob(
                job_id="abcdef1234567890",
                album_id="123456",
                group_id="10001",
                user_id="20001",
                started_at=0,
            )
        }
    )

    await handle_group_message(
        _group_event(
            [
                {"type": "at", "data": {"qq": "12345"}},
                {"type": "text", "data": {"text": " 取消 abcdef12"}},
            ],
            user_id="2456014618",
        ),
        settings,
        state,
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        TaskCollector(),
    )

    assert "abcdef1234567890" in state.cancelled_uploads
    assert backend.admin_cancellations == []
    assert "已请求取消上传" in napcat.sent[-1][1]


@pytest.mark.asyncio
async def test_failed_job_message_includes_error_code(tmp_path: Path) -> None:
    napcat = FakeNapCat()

    await monitor_job(
        "job-123",
        "123456",
        "10001",
        _settings(tmp_path),
        napcat,  # type: ignore[arg-type]
        FakeFailedJobBackend(),  # type: ignore[arg-type]
    )

    assert napcat.sent[-1] == (
        "10001",
        "JM123456 任务失败｡ﾟヽ(ﾟ´Д`)ﾉﾟ｡\n下载失败，请稍后重试\n报错码：JM_DOWNLOAD_FAILED",
    )


@pytest.mark.asyncio
async def test_upload_success(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeDownloadBackend()

    await _download_and_upload(
        {"job_id": "job-123", "filename": "[JM123456]title.pdf"},
        "123456",
        "10001",
        _settings(tmp_path),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
    )

    assert napcat.upload_attempts == 1
    assert napcat.uploads[0][0] == "10001"
    assert napcat.uploads[0][2] == "JM123456.pdf"
    assert napcat.uploads[0][1].parent.name == "_upload"
    assert napcat.uploads[0][1].name == "upload_01.pdf"
    assert napcat.sent[-1] == ("10001", "锵锵！JM123456 已完成啦ʕง•ᴥ•ʔ，请你查收⸜(* ॑꒳ ॑* )⸝")
    assert not (tmp_path / "bot_downloads" / "job-123").exists()


@pytest.mark.asyncio
async def test_upload_can_be_cancelled_by_admin_state(tmp_path: Path) -> None:
    napcat = FakeNapCat()
    backend = FakeDownloadBackend()
    state = BotState(cancelled_uploads={"job-123"})

    await _download_and_upload(
        {"job_id": "job-123", "filename": "[JM123456]title.pdf", "user_id": "20001"},
        "123456",
        "10001",
        _settings(tmp_path),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
        state=state,
    )

    assert napcat.upload_attempts == 0
    assert napcat.sent[-1] == ("10001", "JM123456 上传已由管理员取消。")
    assert state.uploading_jobs == {}
    assert state.cancelled_uploads == set()


@pytest.mark.asyncio
async def test_large_upload_uses_split_parts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    def fake_prepare_upload_files(
        pdf_path: Path,
        _filename: str,
        _max_upload_bytes: int,
        _max_filename_bytes: int,
        _album_id: str | None,
    ) -> list[tuple[Path, str]]:
        part1 = pdf_path.parent / "part1.pdf"
        part2 = pdf_path.parent / "part2.pdf"
        part1.write_bytes(b"%PDF-1.4\npart1")
        part2.write_bytes(b"%PDF-1.4\npart2")
        return [(part1, "part1.pdf"), (part2, "part2.pdf")]

    monkeypatch.setattr(bot_main, "_prepare_upload_files", fake_prepare_upload_files)
    napcat = FakeNapCat()
    backend = FakeDownloadBackend()

    await _download_and_upload(
        {"job_id": "job-123", "filename": "[JM123456]title.pdf"},
        "123456",
        "10001",
        _settings(tmp_path),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
    )

    assert [upload[2] for upload in napcat.uploads] == ["part1.pdf", "part2.pdf"]
    assert "已拆分为 2 个文件上传" in napcat.sent[-2][1]
    assert napcat.sent[-1] == (
        "10001",
        "锵锵！JM123456 已完成啦ʕง•ᴥ•ʔ，由于文件过大，PDF进行了分卷，请你查收⸜(* ॑꒳ ॑* )⸝",
    )


@pytest.mark.asyncio
async def test_upload_retries_until_success(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    napcat = FakeNapCat(upload_failures=2)
    backend = FakeDownloadBackend()

    await _download_and_upload(
        {"job_id": "job-123", "filename": "[JM123456]title.pdf"},
        "123456",
        "10001",
        _settings(tmp_path),
        napcat,  # type: ignore[arg-type]
        backend,  # type: ignore[arg-type]
    )

    assert napcat.upload_attempts == 3
    assert len(napcat.uploads) == 1
    assert "JM123456 已完成" in napcat.sent[-1][1]


@pytest.mark.asyncio
async def test_large_failed_upload_splits_once_after_retries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    def fake_split_for_retry(
        _pdf_path: Path,
        _filename: str,
        _max_filename_bytes: int,
        _album_id: str | None,
    ) -> list[tuple[Path, str]]:
        part1 = tmp_path / "retry-part1.pdf"
        part2 = tmp_path / "retry-part2.pdf"
        part1.write_bytes(b"%PDF-1.4\nretry1")
        part2.write_bytes(b"%PDF-1.4\nretry2")
        return [(part1, "JM123456_part01-of02.pdf"), (part2, "JM123456_part02-of02.pdf")]

    source_pdf = tmp_path / "large.pdf"
    with source_pdf.open("wb") as file:
        file.seek(int(bot_main.DEFAULT_MAX_UPLOAD_BYTES * 0.8))
        file.write(b"\0")

    monkeypatch.setattr(bot_main, "_split_pdf_for_retry", fake_split_for_retry)
    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    napcat = FakeNapCat(upload_failures=bot_main.DEFAULT_UPLOAD_RETRIES)

    ok = await bot_main._upload_item_with_fallback(
        napcat,  # type: ignore[arg-type]
        "10001",
        source_pdf,
        "JM123456.pdf",
        tmp_path,
        "job-123",
        "123456",
        bot_main.MAX_UPLOAD_FILENAME_BYTES,
        bot_main.DEFAULT_UPLOAD_RETRIES,
        label="upload_01",
    )

    assert ok is True
    assert napcat.upload_attempts > bot_main.DEFAULT_UPLOAD_RETRIES
    assert [upload[2] for upload in napcat.uploads] == [
        "JM123456_part01-of02.pdf",
        "JM123456_part02-of02.pdf",
    ]
    assert any("拆得更细" in str(message) for _group_id, message in napcat.sent)
