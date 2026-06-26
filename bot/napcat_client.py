from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake, WebSocketException

logger = logging.getLogger(__name__)


class NapCatError(Exception):
    pass


class NapCatAPIError(NapCatError):
    pass


class NapCatClient:
    def __init__(
        self,
        ws_url: str,
        http_url: str,
        access_token: str | None = None,
        reconnect_seconds: float = 5.0,
    ) -> None:
        self.ws_url = ws_url
        self.http_url = http_url.rstrip("/")
        self.access_token = access_token
        self.reconnect_seconds = reconnect_seconds
        self._client = httpx.AsyncClient(base_url=self.http_url, timeout=60.0)

    async def __aenter__(self) -> "NapCatClient":
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        if not self.access_token:
            return {}
        return {"Authorization": f"Bearer {self.access_token}"}

    async def iter_events(self) -> AsyncIterator[dict[str, Any]]:
        header_arg = "additional_headers"
        while True:
            try:
                async for event in self._iter_events_once(header_arg):
                    yield event
                logger.warning(
                    "NapCat WebSocket %s closed without an error; reconnecting in %.1f seconds.",
                    self.ws_url,
                    self.reconnect_seconds,
                )
                await asyncio.sleep(self.reconnect_seconds)
            except TypeError as exc:
                if header_arg == "additional_headers" and "additional_headers" in str(exc):
                    header_arg = "extra_headers"
                    logger.debug("Falling back to websockets extra_headers parameter.")
                    continue
                else:
                    logger.exception("NapCat WebSocket failed with a TypeError.")
                    await asyncio.sleep(self.reconnect_seconds)
            except asyncio.CancelledError:
                raise
            except ConnectionClosed as exc:
                logger.warning(
                    "NapCat WebSocket %s closed: code=%s, reason=%s; reconnecting in %.1f seconds.",
                    self.ws_url,
                    exc.code,
                    exc.reason or "<empty>",
                    self.reconnect_seconds,
                )
                await asyncio.sleep(self.reconnect_seconds)
            except (InvalidHandshake, OSError, WebSocketException) as exc:
                logger.warning(
                    "NapCat WebSocket %s is unavailable (%s: %s); reconnecting in %.1f seconds.",
                    self.ws_url,
                    exc.__class__.__name__,
                    exc,
                    self.reconnect_seconds,
                )
                await asyncio.sleep(self.reconnect_seconds)
            except Exception:
                logger.exception(
                    "NapCat WebSocket %s failed unexpectedly; reconnecting in %.1f seconds.",
                    self.ws_url,
                    self.reconnect_seconds,
                )
                await asyncio.sleep(self.reconnect_seconds)

    async def _iter_events_once(self, header_arg: str) -> AsyncIterator[dict[str, Any]]:
        headers = self._headers()
        kwargs = {header_arg: headers} if headers else {}
        async with websockets.connect(self.ws_url, **kwargs) as websocket:
            logger.info("Connected to NapCat WebSocket.")
            async for raw_message in websocket:
                try:
                    event = json.loads(raw_message)
                except json.JSONDecodeError:
                    logger.warning("Ignoring invalid NapCat WebSocket payload.")
                    continue
                if isinstance(event, dict):
                    yield event

    async def send_group_msg(self, group_id: str, message: str | list[dict[str, Any]]) -> dict[str, Any]:
        return await self._call_api(
            "/send_group_msg",
            {"group_id": str(group_id), "message": message},
        )

    async def send_group_image(self, group_id: str, image_url: str) -> dict[str, Any]:
        return await self.send_group_msg(
            group_id,
            [{"type": "image", "data": {"file": image_url}}],
        )

    async def upload_group_file(self, group_id: str, file_path: str | Path, name: str) -> dict[str, Any]:
        path = Path(file_path).resolve()
        if not path.is_file():
            raise NapCatAPIError("上传文件不存在")
        return await self._call_api(
            "/upload_group_file",
            {"group_id": str(group_id), "file": str(path), "name": name},
        )

    async def _call_api(self, endpoint: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            response = await self._client.post(endpoint, json=payload, headers=self._headers())
            response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise NapCatAPIError("NapCat HTTP API 调用失败") from exc

        status_value = data.get("status")
        retcode = data.get("retcode")
        if status_value != "ok" or retcode != 0:
            raise NapCatAPIError(f"NapCat API 返回失败：status={status_value}, retcode={retcode}")
        return data
