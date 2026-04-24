"""WebSocket clients for the station's real-time endpoints."""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator
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
            "ws://192.168.1.10:8082",
            galleon_url="https://galleon.example.com",
            station_id="sta_abc123",
        ) as mqtt:
            await mqtt.subscribe(["telem/#", "sensors/power"])
            async for msg in mqtt:
                print(msg.topic, msg.payload)

    Raw station token::

        async with MQTTSubscription(
            "ws://192.168.1.10:8082",
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


# ── Shared base for token-authenticated WebSocket sessions ────────────────────

class _AuthedWebSocket:
    """Mixin handling OAuth/token auth + station_id resolution for ws://."""

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
        self._station_id = station_id
        self._ws: websockets.asyncio.client.ClientConnection | None = None

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

    async def close(self) -> None:
        if self._ws:
            await self._ws.close()
            self._ws = None


class SpeakerMicSession(_AuthedWebSocket):
    """Async context manager for ``/ws/speakers/mic``.

    Sends binary audio frames (any container ``decodebin`` can detect, e.g.
    ``audio/webm;codecs=opus``) to the station's host speakers::

        async with SpeakerMicSession(
            "ws://192.168.1.10:8080", token="ey...",
        ) as spk:
            await spk.send(opus_chunk)
            # or stream from an async iterable:
            await spk.send_stream(chunks)
    """

    async def __aenter__(self) -> SpeakerMicSession:
        await self._resolve_station_id()
        token = await self._get_token()
        self._ws = await websockets.asyncio.client.connect(
            f"{self._base_url}/ws/speakers/mic?token={token}"
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def send(self, frame: bytes) -> None:
        """Send a single binary audio frame."""
        if self._ws is None:
            raise AuthenticationError("not connected")
        await self._ws.send(frame)

    async def send_stream(self, chunks: AsyncIterable[bytes]) -> None:
        """Send each chunk from an async iterable as a binary frame."""
        async for chunk in chunks:
            await self.send(chunk)


class MicWebmStream(_AuthedWebSocket):
    """Async context manager for ``/ws/streams/mic``.

    Yields binary ``audio/webm;codecs=opus`` chunks from the station mic::

        async with MicWebmStream(
            "ws://192.168.1.10:8080", token="ey...",
        ) as mic:
            async for chunk in mic:
                process(chunk)
    """

    async def __aenter__(self) -> MicWebmStream:
        await self._resolve_station_id()
        token = await self._get_token()
        self._ws = await websockets.asyncio.client.connect(
            f"{self._base_url}/ws/streams/mic?token={token}"
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        if self._ws is None:
            raise StopAsyncIteration
        try:
            data = await self._ws.recv()
        except websockets.ConnectionClosed:
            raise StopAsyncIteration
        if isinstance(data, str):
            return data.encode("utf-8")
        return data
