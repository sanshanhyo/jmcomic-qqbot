from __future__ import annotations

import asyncio
import json
import logging
import os
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

from .models import AlbumPreviewResponse, JobCreate, JobCreateResponse, JobResponse
from .task_manager import DuplicateJobError, JobManager, JobManagerConfig

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


@dataclass(frozen=True)
class BackendSettings:
    data_dir: Path
    jmcomic_option_path: Path
    max_concurrent_jobs: int
    job_timeout_seconds: int
    preview_timeout_seconds: int
    job_stall_timeout_seconds: int
    job_progress_check_seconds: float
    backend_api_token: str | None

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
            backend_api_token=os.getenv("BACKEND_API_TOKEN") or None,
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
        job = await _manager(request).create_job(payload.album_id, payload.group_id, payload.user_id)
    except DuplicateJobError as exc:
        existing = exc.existing_job
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": "你已有进行中的任务，或该 JM 编号已在本群下载中",
                "job_id": existing["job_id"],
                "status": existing["status"],
            },
        ) from exc
    return JobCreateResponse(job_id=job["job_id"], status=job["status"])


class PreviewWorkerError(Exception):
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
