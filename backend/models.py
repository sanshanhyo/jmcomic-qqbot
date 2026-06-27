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
    page_count: int | None = Field(default=None, ge=1)


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
    error_code: str | None = None
    downloaded_files: int = 0
    total_files: int = 0
    progress_message: str | None = None


class AlbumPreviewResponse(BaseModel):
    album_id: str
    title: str
    cover_url: str | None = None
    page_count: int | None = None
    page_count_is_estimated: bool = False
    estimated_seconds: int | None = None
    estimated_text: str


class AlbumSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=40)
    page: int = Field(default=1, ge=1, le=5)
    limit: int = Field(default=5, ge=1, le=10)


class AlbumSearchItem(BaseModel):
    album_id: str = Field(pattern=r"^\d{1,12}$")
    title: str
    tags: list[str] = Field(default_factory=list)


class AlbumSearchResponse(BaseModel):
    query: str
    page: int
    total: int
    results: list[AlbumSearchItem]
