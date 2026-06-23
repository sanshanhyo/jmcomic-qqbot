from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class BackendError(Exception):
    pass


class DuplicateJobError(BackendError):
    def __init__(self, job_id: str | None, status: str | None) -> None:
        super().__init__("duplicate job")
        self.job_id = job_id
        self.status = status


class BackendClient:
    def __init__(self, base_url: str, api_token: str | None = None, timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def __aenter__(self) -> "BackendClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        if not self.api_token:
            return {}
        return {"Authorization": f"Bearer {self.api_token}"}

    async def create_job(self, album_id: str, group_id: str, user_id: str) -> dict[str, Any]:
        payload = {"album_id": album_id, "group_id": group_id, "user_id": user_id}
        try:
            response = await self._client.post("/api/jobs", json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise BackendError("后端不可用，请稍后再试") from exc

        if response.status_code == 409:
            detail = self._detail(response)
            raise DuplicateJobError(detail.get("job_id"), detail.get("status"))

        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise BackendError("后端创建任务失败") from exc
        return response.json()

    async def get_job(self, job_id: str) -> dict[str, Any]:
        try:
            response = await self._client.get(f"/api/jobs/{job_id}", headers=self._headers())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BackendError("后端查询任务失败") from exc
        return response.json()

    async def cancel_job(self, job_id: str) -> dict[str, Any]:
        try:
            response = await self._client.post(f"/api/jobs/{job_id}/cancel", headers=self._headers())
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BackendError("后端取消任务失败") from exc
        return response.json()

    async def get_active_job(self, group_id: str, user_id: str) -> dict[str, Any] | None:
        try:
            response = await self._client.get(
                "/api/jobs/active",
                params={"group_id": group_id, "user_id": user_id},
                headers=self._headers(),
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BackendError("后端查询当前任务失败") from exc
        return response.json()

    async def cancel_active_job(self, group_id: str, user_id: str) -> dict[str, Any] | None:
        try:
            response = await self._client.post(
                "/api/jobs/active/cancel",
                params={"group_id": group_id, "user_id": user_id},
                headers=self._headers(),
            )
            if response.status_code == 404:
                return None
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise BackendError("后端取消当前任务失败") from exc
        return response.json()

    async def get_album_preview(self, album_id: str) -> dict[str, Any]:
        try:
            response = await self._client.get(f"/api/albums/{album_id}/preview", headers=self._headers())
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            message = self._error_detail_message(exc.response) or "获取漫画信息失败"
            raise BackendError(message) from exc
        except httpx.HTTPError as exc:
            raise BackendError("后端不可用，请稍后再试") from exc
        return response.json()

    async def download_file(self, job_id: str, dest_path: str | Path) -> Path:
        dest = Path(dest_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest.with_suffix(dest.suffix + ".part")

        try:
            async with self._client.stream(
                "GET",
                f"/api/jobs/{job_id}/file",
                headers=self._headers(),
            ) as response:
                response.raise_for_status()
                with tmp_path.open("wb") as file:
                    async for chunk in response.aiter_bytes():
                        file.write(chunk)
        except httpx.HTTPError as exc:
            tmp_path.unlink(missing_ok=True)
            raise BackendError("PDF 下载失败") from exc

        if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
            tmp_path.unlink(missing_ok=True)
            raise BackendError("PDF 下载失败：文件为空")

        tmp_path.replace(dest)
        return dest

    @staticmethod
    def _detail(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            return {}
        detail = data.get("detail")
        return detail if isinstance(detail, dict) else {}

    @staticmethod
    def _error_detail_message(response: httpx.Response) -> str | None:
        try:
            data = response.json()
        except ValueError:
            return None
        detail = data.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, dict):
            message = detail.get("message")
            return message if isinstance(message, str) else None
        return None
