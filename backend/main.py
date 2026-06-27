from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse

from .models import AlbumPreviewResponse, AlbumSearchRequest, AlbumSearchResponse, JobCreate, JobCreateResponse, JobResponse
from .task_manager import ActiveJobLimitError, DuplicateJobError, JobManager, JobManagerConfig

logger = logging.getLogger(__name__)


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Invalid integer for %s; using %s.", name, default)
        return default


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        logger.warning("Invalid float for %s; using %s.", name, default)
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class BackendSettings:
    data_dir: Path
    jmcomic_option_path: Path
    max_concurrent_jobs: int
    job_timeout_seconds: int
    preview_timeout_seconds: int
    job_stall_timeout_seconds: int
    job_progress_check_seconds: float
    cache_cleanup_interval_seconds: int
    job_cache_ttl_seconds: int
    bot_download_cache_ttl_seconds: int
    preview_cache_ttl_seconds: int
    backend_api_token: str | None
    enable_search: bool
    search_timeout_seconds: int
    search_result_limit: int
    max_active_jobs_per_group: int
    max_active_jobs_per_user: int

    @classmethod
    def from_env(cls) -> "BackendSettings":
        load_dotenv()
        return cls(
            data_dir=Path(os.getenv("DATA_DIR", "./data")),
            jmcomic_option_path=Path(os.getenv("JMCOMIC_OPTION_PATH", "./config/jmcomic-option.yml")),
            max_concurrent_jobs=max(1, _env_int("MAX_CONCURRENT_JOBS", 1)),
            job_timeout_seconds=max(1, _env_int("JOB_TIMEOUT_SECONDS", 1800)),
            preview_timeout_seconds=max(1, _env_int("PREVIEW_TIMEOUT_SECONDS", 30)),
            job_stall_timeout_seconds=max(0, _env_int("JOB_STALL_TIMEOUT_SECONDS", 300)),
            job_progress_check_seconds=max(1.0, _env_float("JOB_PROGRESS_CHECK_SECONDS", 10.0)),
            cache_cleanup_interval_seconds=max(0, _env_int("CACHE_CLEANUP_INTERVAL_SECONDS", 3600)),
            job_cache_ttl_seconds=max(0, _env_int("JOB_CACHE_TTL_SECONDS", 259200)),
            bot_download_cache_ttl_seconds=max(0, _env_int("BOT_DOWNLOAD_CACHE_TTL_SECONDS", 259200)),
            preview_cache_ttl_seconds=max(0, _env_int("PREVIEW_CACHE_TTL_SECONDS", 86400)),
            backend_api_token=os.getenv("BACKEND_API_TOKEN") or None,
            enable_search=_env_bool("ENABLE_SEARCH", False),
            search_timeout_seconds=max(1, _env_int("SEARCH_TIMEOUT_SECONDS", 20)),
            search_result_limit=max(1, min(10, _env_int("SEARCH_RESULT_LIMIT", 5))),
            max_active_jobs_per_group=max(0, _env_int("MAX_ACTIVE_JOBS_PER_GROUP", 3)),
            max_active_jobs_per_user=max(0, _env_int("MAX_ACTIVE_JOBS_PER_USER", 1)),
        )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    load_dotenv()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = BackendSettings.from_env()
    manager = JobManager(
        JobManagerConfig(
            data_dir=settings.data_dir,
            option_path=settings.jmcomic_option_path,
            max_concurrent_jobs=settings.max_concurrent_jobs,
            job_timeout_seconds=settings.job_timeout_seconds,
            job_stall_timeout_seconds=settings.job_stall_timeout_seconds,
            progress_interval_seconds=settings.job_progress_check_seconds,
            cache_cleanup_interval_seconds=settings.cache_cleanup_interval_seconds,
            job_cache_ttl_seconds=settings.job_cache_ttl_seconds,
            bot_download_cache_ttl_seconds=settings.bot_download_cache_ttl_seconds,
            preview_cache_ttl_seconds=settings.preview_cache_ttl_seconds,
            max_active_jobs_per_group=settings.max_active_jobs_per_group,
            max_active_jobs_per_user=settings.max_active_jobs_per_user,
        )
    )
    app.state.settings = settings
    app.state.job_manager = manager
    await manager.start()
    try:
        yield
    finally:
        await manager.stop()


app = FastAPI(title="QQBot JMComic Backend", lifespan=lifespan)


def _manager(request: Request) -> JobManager:
    return request.app.state.job_manager


def _require_api_token(request: Request, authorization: str | None) -> None:
    token = request.app.state.settings.backend_api_token
    if not token:
        return
    if authorization != f"Bearer {token}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/jobs", response_model=JobCreateResponse)
async def create_job(
    payload: JobCreate,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JobCreateResponse:
    _require_api_token(request, authorization)
    try:
        job = await _manager(request).create_job(payload.album_id, payload.group_id, payload.user_id, payload.page_count)
    except DuplicateJobError as exc:
        existing = exc.existing_job
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "你已有进行中的任务，或该 JM 编号已在本群下载中",
                "error_code": "DUPLICATE_ACTIVE_JOB",
                "job_id": existing["job_id"],
                "status": existing["status"],
            },
        ) from exc
    except ActiveJobLimitError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "message": exc.user_message,
                "error_code": exc.error_code,
            },
        ) from exc
    return JobCreateResponse(job_id=job["job_id"], status=job["status"])


class PreviewWorkerError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


class SearchWorkerError(Exception):
    def __init__(self, user_message: str) -> None:
        super().__init__(user_message)
        self.user_message = user_message


@app.get("/api/albums/{album_id}/preview", response_model=AlbumPreviewResponse)
async def get_album_preview(
    album_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> AlbumPreviewResponse:
    _require_api_token(request, authorization)
    if not album_id.isdigit() or len(album_id) > 12:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid album_id")

    settings: BackendSettings = request.app.state.settings
    result_path = settings.data_dir.resolve() / "previews" / f"{uuid.uuid4()}.json"
    try:
        preview = await _run_preview_worker(
            album_id,
            settings.jmcomic_option_path,
            result_path,
            settings.preview_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="获取漫画信息超时，请稍后重试",
        ) from exc
    except PreviewWorkerError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.user_message) from exc
    finally:
        result_path.unlink(missing_ok=True)

    return AlbumPreviewResponse(**preview)


@app.post("/api/search", response_model=AlbumSearchResponse)
async def search_albums(
    payload: AlbumSearchRequest,
    request: Request,
    authorization: str | None = Header(default=None),
) -> AlbumSearchResponse:
    _require_api_token(request, authorization)
    settings: BackendSettings = request.app.state.settings
    if not settings.enable_search:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="搜索功能未启用")

    result_path = settings.data_dir.resolve() / "searches" / f"{uuid.uuid4()}.json"
    limit = min(payload.limit, settings.search_result_limit)
    try:
        result = await _run_search_worker(
            payload.query,
            payload.page,
            limit,
            settings.jmcomic_option_path,
            result_path,
            settings.search_timeout_seconds,
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="搜索超时，请稍后重试",
        ) from exc
    except SearchWorkerError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=exc.user_message) from exc
    finally:
        result_path.unlink(missing_ok=True)

    return AlbumSearchResponse(**result)


async def _run_preview_worker(
    album_id: str,
    option_path: Path,
    result_path: Path,
    timeout_seconds: int,
) -> dict:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "backend.preview_worker",
        "--album-id",
        album_id,
        "--option-path",
        str(option_path),
        "--result-path",
        str(result_path),
    ]
    kwargs: dict[str, object] = {
        "stdout": asyncio.subprocess.DEVNULL,
        "stderr": asyncio.subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    process = await asyncio.create_subprocess_exec(*command, **kwargs)
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        await _terminate_process(process)
        raise

    if not result_path.is_file():
        raise PreviewWorkerError(f"获取漫画信息失败，退出码：{process.returncode}")

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise PreviewWorkerError("获取漫画信息结果无效") from exc

    if not result.get("ok"):
        raise PreviewWorkerError(result.get("user_message") or "获取漫画信息失败，请稍后重试")
    preview = result.get("preview")
    if not isinstance(preview, dict):
        raise PreviewWorkerError("获取漫画信息结果无效")
    return preview


async def _run_search_worker(
    query: str,
    page: int,
    limit: int,
    option_path: Path,
    result_path: Path,
    timeout_seconds: int,
) -> dict:
    result_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        sys.executable,
        "-m",
        "backend.search_worker",
        "--query",
        query,
        "--page",
        str(page),
        "--limit",
        str(limit),
        "--option-path",
        str(option_path),
        "--result-path",
        str(result_path),
    ]
    kwargs: dict[str, object] = {
        "stdout": asyncio.subprocess.DEVNULL,
        "stderr": asyncio.subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True

    process = await asyncio.create_subprocess_exec(*command, **kwargs)
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout_seconds)
    except asyncio.TimeoutError:
        await _terminate_process(process)
        raise

    if not result_path.is_file():
        raise SearchWorkerError(f"搜索失败，退出码：{process.returncode}")

    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise SearchWorkerError("搜索结果无效") from exc

    if not result.get("ok"):
        raise SearchWorkerError(result.get("user_message") or "搜索失败，请稍后重试")
    search_result = result.get("result")
    if not isinstance(search_result, dict):
        raise SearchWorkerError("搜索结果无效")
    return search_result


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
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
        pass

    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    await process.wait()


@app.get("/api/admin/status")
async def admin_status(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_api_token(request, authorization)
    settings: BackendSettings = request.app.state.settings
    manager = _manager(request)
    snapshot = await _collect_admin_status(settings.data_dir, manager)
    return snapshot


@app.get("/api/admin/queue")
async def admin_queue(
    request: Request,
    authorization: str | None = Header(default=None),
    limit: int = 20,
) -> dict:
    _require_api_token(request, authorization)
    jobs = await asyncio.to_thread(_manager(request).list_admin_jobs, limit)
    return {"jobs": jobs}


@app.post("/api/admin/cache/cleanup")
async def admin_cleanup_cache(
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_api_token(request, authorization)
    manager = _manager(request)
    active_count = await asyncio.to_thread(manager.count_active_jobs)
    if active_count > 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "当前还有下载或转换任务运行，暂不清理缓存",
                "error_code": "ACTIVE_JOBS_RUNNING",
                "active_count": active_count,
            },
        )

    before = await asyncio.to_thread(_directory_size, manager.data_dir)
    stats = await manager.cleanup_cache_once()
    after = await asyncio.to_thread(_directory_size, manager.data_dir)
    return {"stats": stats, "freed_bytes": max(0, before - after)}


@app.post("/api/admin/jobs/{target}/cancel")
async def admin_cancel_job(
    target: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> dict:
    _require_api_token(request, authorization)
    manager = _manager(request)
    job = await asyncio.to_thread(manager.find_job_by_prefix, target)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found or ambiguous")

    cancelled = await manager.cancel_job(str(job["job_id"]), "任务已由管理员取消")
    if cancelled is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return {"job": cancelled}


async def _collect_admin_status(data_dir: Path, manager: JobManager) -> dict:
    cpu_task = asyncio.create_task(_cpu_percent())
    net_before = await asyncio.to_thread(_read_network_bytes)
    await asyncio.sleep(1)
    net_after = await asyncio.to_thread(_read_network_bytes)
    cpu_percent = await cpu_task

    system_status = await asyncio.to_thread(_collect_admin_status_sync, data_dir, manager)
    system_status["cpu_percent"] = cpu_percent
    system_status["network"] = _network_speed(net_before, net_after)
    return system_status


def _collect_admin_status_sync(data_dir: Path, manager: JobManager) -> dict:
    disk = shutil.disk_usage(data_dir.resolve())
    data_dir = data_dir.resolve()
    jobs = manager.list_admin_jobs(50)
    counts = {
        "queued": 0,
        "downloading": 0,
        "converting": 0,
        "failed": 0,
    }
    for job in jobs:
        status_value = str(job.get("status") or "")
        if status_value in counts:
            counts[status_value] += 1

    return {
        "memory": _memory_status(),
        "disk": {
            "total": disk.total,
            "used": disk.used,
            "free": disk.free,
        },
        "cache": {
            "data": _directory_size(data_dir),
            "jobs": _directory_size(data_dir / "jobs"),
            "bot_downloads": _directory_size(data_dir / "bot_downloads"),
            "previews": _directory_size(data_dir / "previews"),
            "cover_cache": _directory_size(data_dir / "cover_cache"),
        },
        "jobs": {
            "active": manager.count_active_jobs(),
            **counts,
        },
    }


async def _cpu_percent() -> float | None:
    first = await asyncio.to_thread(_read_cpu_times)
    if first is None:
        return None
    await asyncio.sleep(1)
    second = await asyncio.to_thread(_read_cpu_times)
    if second is None:
        return None
    idle_delta = second["idle"] - first["idle"]
    total_delta = second["total"] - first["total"]
    if total_delta <= 0:
        return None
    busy_delta = max(0, total_delta - idle_delta)
    return round(busy_delta * 100 / total_delta, 1)


def _read_cpu_times() -> dict[str, int] | None:
    stat_path = Path("/proc/stat")
    if not stat_path.is_file():
        return None
    try:
        first_line = stat_path.read_text(encoding="utf-8").splitlines()[0]
        parts = [int(part) for part in first_line.split()[1:]]
    except (IndexError, OSError, ValueError):
        return None
    if len(parts) < 5:
        return None
    idle = parts[3] + parts[4]
    return {"idle": idle, "total": sum(parts)}


def _memory_status() -> dict[str, int] | None:
    meminfo_path = Path("/proc/meminfo")
    if not meminfo_path.is_file():
        return None
    values: dict[str, int] = {}
    try:
        for line in meminfo_path.read_text(encoding="utf-8").splitlines():
            key, raw_value = line.split(":", 1)
            number = int(raw_value.strip().split()[0]) * 1024
            values[key] = number
    except (OSError, ValueError, IndexError):
        return None
    total = values.get("MemTotal")
    available = values.get("MemAvailable")
    if total is None or available is None:
        return None
    return {"total": total, "available": available, "used": max(0, total - available)}


def _read_network_bytes() -> dict[str, int] | None:
    dev_path = Path("/proc/net/dev")
    if not dev_path.is_file():
        return None
    rx_total = 0
    tx_total = 0
    try:
        for line in dev_path.read_text(encoding="utf-8").splitlines()[2:]:
            if ":" not in line:
                continue
            interface, raw_data = line.split(":", 1)
            if interface.strip() == "lo":
                continue
            parts = raw_data.split()
            rx_total += int(parts[0])
            tx_total += int(parts[8])
    except (OSError, ValueError, IndexError):
        return None
    return {"rx": rx_total, "tx": tx_total}


def _network_speed(before: dict[str, int] | None, after: dict[str, int] | None) -> dict[str, float | None]:
    if before is None or after is None:
        return {"rx_bytes_per_second": None, "tx_bytes_per_second": None}
    return {
        "rx_bytes_per_second": max(0, after["rx"] - before["rx"]),
        "tx_bytes_per_second": max(0, after["tx"] - before["tx"]),
    }


def _directory_size(path: Path) -> int:
    path = path.resolve()
    if not path.exists():
        return 0
    if path.is_file():
        try:
            return path.stat().st_size
        except OSError:
            return 0

    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file():
                total += child.stat().st_size
        except OSError:
            continue
    return total


@app.get("/api/jobs/active", response_model=JobResponse)
async def get_active_job(
    group_id: str,
    user_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JobResponse:
    _require_api_token(request, authorization)
    if not group_id.isdigit() or not user_id.isdigit():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid group_id or user_id")
    job = _manager(request).find_active_job_for_user(group_id, user_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="active job not found")
    return JobResponse(**job)


@app.post("/api/jobs/active/cancel", response_model=JobResponse)
async def cancel_active_job(
    group_id: str,
    user_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JobResponse:
    _require_api_token(request, authorization)
    if not group_id.isdigit() or not user_id.isdigit():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="invalid group_id or user_id")
    job = await _manager(request).cancel_active_job_for_user(group_id, user_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="active job not found")
    return JobResponse(**job)


@app.get("/api/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JobResponse:
    _require_api_token(request, authorization)
    job = _manager(request).get_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return JobResponse(**job)


@app.post("/api/jobs/{job_id}/cancel", response_model=JobResponse)
async def cancel_job(
    job_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> JobResponse:
    _require_api_token(request, authorization)
    job = await _manager(request).cancel_job(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="job not found")
    return JobResponse(**job)


@app.get("/api/jobs/{job_id}/file")
async def download_file(
    job_id: str,
    request: Request,
    authorization: str | None = Header(default=None),
) -> FileResponse:
    _require_api_token(request, authorization)
    result = _manager(request).get_completed_file(job_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file not ready")
    file_path, filename = result
    return FileResponse(path=file_path, filename=filename, media_type="application/pdf")


def main() -> None:
    load_dotenv()
    uvicorn.run(
        "backend.main:app",
        host=os.getenv("BACKEND_HOST", "127.0.0.1"),
        port=_env_int("BACKEND_PORT", 8000),
    )


if __name__ == "__main__":
    main()
