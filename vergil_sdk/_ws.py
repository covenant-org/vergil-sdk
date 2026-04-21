"""WebSocket client for the MQTT message bridge endpoint."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import websockets
import websockets.asyncio.client

from .exceptions import AuthenticationError
from .models import MQTTAck, MQTTMessage
from .oauth import OAuthTokenManager


class MQTTSubscription:
    """Async context manager for the ``/ws/mqtt`` WebSocket endpoint.

    Two auth modes (mirror :class:`vergil_sdk.VergilClient`):

    OAuth (via cached credentials from ``vergil login``)::

        async with MQTTSubscription(
            "ws://192.168.1.10:8080",
            galleon_url="https://galleon.example.com",
            station_id="sta_abc123",
        ) as mqtt:
            await mqtt.subscribe(["telem/#", "sensors/power"])
            async for msg in mqtt:
                print(msg.topic, msg.payload)

    Raw station token::

        async with MQTTSubscription(
            "ws://192.168.1.10:8080",
            token="ey...",
        ) as mqtt:
            await mqtt.subscribe(["telem/#"])
            async for msg in mqtt:
                print(msg.topic, msg.payload)
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        galleon_url: str | None = None,
        station_id: str | None = None,
        credentials_path: Path | None = None,
        interactive: bool = False,
    ) -> None:
        base = base_url.rstrip("/")
        if base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        elif base.startswith("https://"):
            base = "wss://" + base[len("https://"):]

        self._base_url = base
        self._static_token = token
        self._ws: websockets.asyncio.client.ClientConnection | None = None
        self._station_id = station_id

        if galleon_url:
            self._token_mgr: OAuthTokenManager | None = OAuthTokenManager(
                galleon_url,
                station_id or "",
                credentials_path=credentials_path,
                interactive=interactive,
            )
        else:
            self._token_mgr = None

    async def _get_token(self) -> str:
        if self._token_mgr:
            return await self._token_mgr.get_token_async()
        if self._static_token:
            return self._static_token
        raise AuthenticationError("no token configured")

    async def _resolve_station_id(self) -> None:
        if self._station_id is not None or self._token_mgr is None:
            return
        http_base = self._base_url
        if http_base.startswith("ws://"):
            http_base = "http://" + http_base[len("ws://"):]
        elif http_base.startswith("wss://"):
            http_base = "https://" + http_base[len("wss://"):]

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{http_base}/health", timeout=10.0)
            data = resp.json()
            self._station_id = data["id"]
            self._token_mgr.set_station_id(self._station_id)

    async def __aenter__(self) -> MQTTSubscription:
        await self._resolve_station_id()
        token = await self._get_token()
        self._ws = await websockets.asyncio.client.connect(
            f"{self._base_url}/ws/mqtt?token={token}"
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def subscribe(self, topics: list[str]) -> MQTTAck:
        """Subscribe to one or more MQTT topic patterns."""
        await self._ws.send(json.dumps({"subscribe": topics}))
        raw = json.loads(await self._ws.recv())
        return MQTTAck(action=raw.get("ack", ""), topics=raw.get("topics", []))

    async def unsubscribe(self, topics: list[str]) -> MQTTAck:
        """Unsubscribe from one or more MQTT topic patterns."""
        await self._ws.send(json.dumps({"unsubscribe": topics}))
        raw = json.loads(await self._ws.recv())
        return MQTTAck(action=raw.get("ack", ""), topics=raw.get("topics", []))

    def __aiter__(self) -> AsyncIterator[MQTTMessage]:
        return self

    async def __anext__(self) -> MQTTMessage:
        if self._ws is None:
            raise StopAsyncIteration
        try:
            raw = await self._ws.recv()
        except websockets.ConnectionClosed:
            raise StopAsyncIteration
        data = json.loads(raw)
        if "ack" in data:
            return await self.__anext__()
        return MQTTMessage(
            topic=data["topic"],
            payload=data["payload"],
            timestamp=data["timestamp"],
        )
