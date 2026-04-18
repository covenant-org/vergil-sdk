"""WebSocket client for the MQTT message bridge endpoint."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

import jwt
import websockets
import websockets.asyncio.client

from .exceptions import AuthenticationError
from .models import MQTTAck, MQTTMessage

EXPIRY_SKEW_SECONDS = 60


class MQTTSubscription:
    """Async context manager for the ``/ws/mqtt`` WebSocket endpoint.

    Handles authentication with either a developer token (auto-exchanged
    and refreshed) or a raw station token.

    Usage with developer token (recommended)::

        async with MQTTSubscription(
            "ws://192.168.1.10:8080",
            galleon_url="https://galleon.example.com",
            station_id="sta_abc123",
            developer_token="dev_ey...",
        ) as mqtt:
            await mqtt.subscribe(["telem/#", "sensors/power"])
            async for msg in mqtt:
                print(msg.topic, msg.payload)

    Usage with raw station token::

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
        developer_token: str | None = None,
    ) -> None:
        base = base_url.rstrip("/")
        if base.startswith("http://"):
            base = "ws://" + base[len("http://"):]
        elif base.startswith("https://"):
            base = "wss://" + base[len("https://"):]

        self._base_url = base
        self._static_token = token
        self._ws: websockets.asyncio.client.ClientConnection | None = None

        if developer_token:
            if not galleon_url:
                raise ValueError(
                    "galleon_url is required when using developer_token"
                )
            self._galleon_url = galleon_url.rstrip("/")
            self._station_id = station_id
            self._developer_token = developer_token
            self._station_token: str | None = None
            self._expires_at: float = 0.0
        else:
            self._galleon_url = None
            self._station_id = None
            self._developer_token = None
            self._station_token = None
            self._expires_at = 0.0

    async def _get_token(self) -> str:
        """Return a valid station token, exchanging if needed."""
        if self._developer_token:
            if (
                self._station_token is None
                or (self._expires_at - time.time()) <= EXPIRY_SKEW_SECONDS
            ):
                await self._exchange()
            return self._station_token
        if self._static_token:
            return self._static_token
        raise AuthenticationError("no token configured")

    async def _exchange(self) -> None:
        import httpx

        url = f"{self._galleon_url}/api/auth/token"
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {self._developer_token}",
                    },
                    json={"station_id": self._station_id},
                    timeout=10.0,
                )
            except httpx.HTTPError as e:
                raise AuthenticationError(
                    f"token exchange failed: {e}") from e

        if resp.status_code != 200:
            raise AuthenticationError(
                f"token exchange failed ({resp.status_code}): {resp.text}",
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
            self._station_token = data["token"]
        except (ValueError, KeyError) as e:
            raise AuthenticationError(
                f"unexpected exchange response: {e}") from e

        try:
            claims = jwt.decode(
                self._station_token, options={"verify_signature": False})
            self._expires_at = float(claims.get("exp", 0))
        except jwt.InvalidTokenError:
            self._expires_at = 0.0

    async def _resolve_station_id(self) -> None:
        """Fetch station_id from /health if it was not provided."""
        if self._station_id is not None:
            return
        import httpx

        # /health requires no auth, use the http base url
        http_base = self._base_url
        if http_base.startswith("ws://"):
            http_base = "http://" + http_base[len("ws://"):]
        elif http_base.startswith("wss://"):
            http_base = "https://" + http_base[len("wss://"):]

        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{http_base}/health", timeout=10.0)
            data = resp.json()
            self._station_id = data["id"]

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
