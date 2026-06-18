from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class JobStatus(StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    COMPLETED = "completed"
    FAILED = "failed"


class JobCreate(BaseModel):
    album_id: str = Field(pattern=r"^\d{1,12}$")
    group_id: str = Field(pattern=r"^\d+$")
    user_id: str = Field(pattern=r"^\d+$")


class JobCreateResponse(BaseModel):
    job_id: str
    status: JobStatus


class JobResponse(BaseModel):
    job_id: str
    album_id: str
    group_id: str
    user_id: str
    status: JobStatus
    filename: str | None = None
    error_message: str | None = None
    downloaded_files: int = 0
    progress_message: str | None = None
