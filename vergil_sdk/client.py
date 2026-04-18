"""Synchronous and asynchronous HTTP clients for the Vergil station API."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import httpx
import jwt

from .exceptions import ConnectionError as VergilConnectionError
from .exceptions import raise_for_status, AuthenticationError
from .models import (
    CraneCommandResult,
    CraneStatus,
    FrigateEvent,
    FrigateEventList,
    HealthStatus,
    Metrics,
    RecordingSegment,
    ReviewSegment,
    ReviewSegmentList,
    SegmentList,
    SensorData,
    Stream,
)

DEFAULT_TIMEOUT = 30.0
VIDEO_TIMEOUT = 120.0
EXPIRY_SKEW_SECONDS = 60


# ── Helpers ────────────────────────────────────────────────────────────────────

def _check(resp: httpx.Response) -> dict:
    """Parse JSON and raise on error status codes."""
    if resp.status_code >= 400:
        try:
            body = resp.json()
        except Exception:
            body = {"error": resp.text}
        raise_for_status(resp.status_code, body)
    return resp.json()


def _strip_none(params: dict) -> dict:
    return {k: v for k, v in params.items() if v is not None}


def _parse_streams(data: dict) -> list[Stream]:
    return [
        Stream(
            camera_id=s["camera_id"],
            rtsp_uri=s["rtsp_uri"],
            port=s["port"],
        )
        for s in data.get("streams", [])
    ]


def _parse_event(raw: dict) -> FrigateEvent:
    return FrigateEvent(raw=raw)


def _parse_event_list(data: dict) -> FrigateEventList:
    return FrigateEventList(
        events=[_parse_event(e) for e in data.get("events", [])],
        count=data.get("count", 0),
    )


def _parse_review(raw: dict) -> ReviewSegment:
    return ReviewSegment(raw=raw)


def _parse_review_list(data: dict) -> ReviewSegmentList:
    return ReviewSegmentList(
        reviewsegments=[
            _parse_review(r) for r in data.get("reviewsegments", [])
        ],
        count=data.get("count", 0),
    )


def _parse_segments(data: dict) -> SegmentList:
    return SegmentList(
        segments=[
            RecordingSegment(
                camera=s["camera"],
                start_time=s["start_time"],
                end_time=s["end_time"],
                duration=s["duration"],
                path=s["path"],
            )
            for s in data.get("segments", [])
        ],
        count=data.get("count", 0),
    )


def _extract_expiry(token: str) -> float:
    """Read the ``exp`` claim without verifying the signature."""
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
        return float(claims.get("exp", 0))
    except jwt.InvalidTokenError:
        return 0.0


# ── Token management ───────────────────────────────────────────────────────────

class _AsyncTokenManager:
    """Handles developer-token to station-token exchange (async).

    Caches the station token and transparently re-exchanges before it
    expires so callers never see a 401.
    """

    def __init__(
        self, galleon_url: str, station_id: str | None, developer_token: str,
    ) -> None:
        self._exchange_url = f"{galleon_url.rstrip('/')}/api/auth/token"
        self._station_id = station_id
        self._developer_token = developer_token
        self._station_token: str | None = None
        self._expires_at: float = 0.0

    def set_station_id(self, station_id: str) -> None:
        self._station_id = station_id

    async def get_token(self) -> str:
        if self._station_token is None or self._near_expiry():
            await self._exchange()
        return self._station_token

    def _near_expiry(self) -> bool:
        return (self._expires_at - time.time()) <= EXPIRY_SKEW_SECONDS

    async def _exchange(self) -> None:
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.post(
                    self._exchange_url,
                    headers={
                        "Authorization": f"Bearer {self._developer_token}",
                    },
                    json={"station_id": self._station_id},
                    timeout=10.0,
                )
            except httpx.HTTPError as e:
                raise AuthenticationError(
                    f"token exchange request failed: {e}") from e

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

        self._expires_at = _extract_expiry(self._station_token)


class _SyncTokenManager:
    """Handles developer-token to station-token exchange (sync)."""

    def __init__(
        self, galleon_url: str, station_id: str | None, developer_token: str,
    ) -> None:
        self._exchange_url = f"{galleon_url.rstrip('/')}/api/auth/token"
        self._station_id = station_id
        self._developer_token = developer_token
        self._station_token: str | None = None
        self._expires_at: float = 0.0

    def set_station_id(self, station_id: str) -> None:
        self._station_id = station_id

    def get_token(self) -> str:
        if self._station_token is None or self._near_expiry():
            self._exchange()
        return self._station_token

    def _near_expiry(self) -> bool:
        return (self._expires_at - time.time()) <= EXPIRY_SKEW_SECONDS

    def _exchange(self) -> None:
        try:
            resp = httpx.post(
                self._exchange_url,
                headers={
                    "Authorization": f"Bearer {self._developer_token}",
                },
                json={"station_id": self._station_id},
                timeout=10.0,
            )
        except httpx.HTTPError as e:
            raise AuthenticationError(
                f"token exchange request failed: {e}") from e

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

        self._expires_at = _extract_expiry(self._station_token)


# ── Async client ───────────────────────────────────────────────────────────────

class AsyncVergilClient:
    """Async client for the Vergil station local API.

    Authenticate with a developer token (recommended) — the SDK handles
    the exchange and auto-refresh transparently.  ``station_id`` is
    optional; when omitted the SDK auto-discovers it from ``/health``::

        async with AsyncVergilClient(
            "http://192.168.1.10:8080",
            galleon_url="https://galleon.example.com",
            developer_token="dev_ey...",
        ) as client:
            health = await client.health()

    Or pass a raw station token directly (you manage expiry yourself)::

        async with AsyncVergilClient(
            "http://192.168.1.10:8080",
            token="ey...",
        ) as client:
            ...
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        galleon_url: str | None = None,
        station_id: str | None = None,
        developer_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._static_token = token
        self._client: httpx.AsyncClient | None = None
        self._station_id_resolved = station_id is not None

        if developer_token:
            if not galleon_url:
                raise ValueError(
                    "galleon_url is required when using developer_token"
                )
            self._token_mgr: _AsyncTokenManager | None = _AsyncTokenManager(
                galleon_url, station_id, developer_token,
            )
        else:
            self._token_mgr = None

    async def _resolve_station_id(self) -> None:
        """Fetch station_id from /health if it was not provided."""
        if self._station_id_resolved:
            return
        self._station_id_resolved = True
        data = await self._get("/health")
        self._token_mgr.set_station_id(data["id"])

    async def _get_auth_headers(self) -> dict[str, str]:
        if self._token_mgr:
            await self._resolve_station_id()
            token = await self._token_mgr.get_token()
            return {"Authorization": f"Bearer {token}"}
        if self._static_token:
            return {"Authorization": f"Bearer {self._static_token}"}
        return {}

    def _build_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout,
        )

    async def __aenter__(self) -> AsyncVergilClient:
        self._client = self._build_client()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    @property
    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    async def _get(self, path: str, **params: Any) -> dict:
        try:
            resp = await self._http.get(
                path,
                params=_strip_none(params),
                headers=await self._get_auth_headers(),
            )
        except httpx.ConnectError as e:
            raise VergilConnectionError(str(e)) from e
        return _check(resp)

    async def _get_bytes(
        self, path: str, timeout: float | None = None, **params: Any,
    ) -> bytes:
        try:
            resp = await self._http.get(
                path,
                params=_strip_none(params),
                headers=await self._get_auth_headers(),
                timeout=timeout or VIDEO_TIMEOUT,
            )
        except httpx.ConnectError as e:
            raise VergilConnectionError(str(e)) from e
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = {"error": resp.text}
            raise_for_status(resp.status_code, body)
        return resp.content

    async def _post(self, path: str, json: dict) -> dict:
        try:
            resp = await self._http.post(
                path,
                json=json,
                headers=await self._get_auth_headers(),
            )
        except httpx.ConnectError as e:
            raise VergilConnectionError(str(e)) from e
        return _check(resp)

    # ── Health ─────────────────────────────────────────────────────────────

    async def health(self) -> HealthStatus:
        """Check station liveness (no auth required)."""
        data = await self._get("/health")
        return HealthStatus(status=data["status"], id=data["id"])

    # ── Metrics ────────────────────────────────────────────────────────────

    async def get_metrics(self) -> Metrics:
        """Get system metrics (CPU, GPU, RAM, storage, network)."""
        data = await self._get("/metrics")
        return Metrics(
            computing=data.get("computing", {}),
            last_report_ts=data.get("last_report_ts"),
        )

    # ── Sensors ────────────────────────────────────────────────────────────

    async def get_sensors(self) -> SensorData:
        """Get all sensor telemetry (power and battery)."""
        data = await self._get("/sensors")
        return SensorData(
            power=data.get("power", {}),
            battery=data.get("battery", {}),
        )

    async def get_power(self) -> dict[str, Any]:
        """Get power sensor data."""
        return await self._get("/sensors/power")

    async def get_battery(self) -> dict[str, Any]:
        """Get battery sensor data."""
        return await self._get("/sensors/battery")

    # ── Crane ──────────────────────────────────────────────────────────────

    async def get_crane_status(self) -> CraneStatus:
        """Get current crane status."""
        data = await self._get("/crane")
        return CraneStatus(raw=data)

    async def send_crane_command(
        self, command: str,
    ) -> CraneCommandResult:
        """Send a crane command: 'crane_up', 'crane_down', or 'crane_stop'."""
        data = await self._post("/crane", json={"command": command})
        return CraneCommandResult(command=data["command"])

    # ── Streams ────────────────────────────────────────────────────────────

    async def list_streams(self) -> list[Stream]:
        """List all active camera RTSP proxy URIs."""
        data = await self._get("/streams")
        return _parse_streams(data)

    async def get_stream(self, camera_id: str) -> Stream:
        """Get the RTSP proxy URI for a single camera."""
        data = await self._get(f"/streams/{camera_id}")
        return Stream(
            camera_id=data["camera_id"],
            rtsp_uri=data["rtsp_uri"],
            port=data["port"],
        )

    async def stream_mic(self) -> httpx.Response:
        """Open a streaming connection to the live mic audio endpoint.

        Returns the raw httpx.Response with ``audio/ogg`` streaming body.
        Caller is responsible for reading and closing the stream::

            resp = await client.stream_mic()
            async for chunk in resp.aiter_bytes():
                process(chunk)
            await resp.aclose()
        """
        try:
            req = self._http.build_request(
                "GET", "/streams/mic",
                headers=await self._get_auth_headers(),
            )
            resp = await self._http.send(req, stream=True)
        except httpx.ConnectError as e:
            raise VergilConnectionError(str(e)) from e
        if resp.status_code >= 400:
            raise_for_status(resp.status_code, {"error": "mic stream failed"})
        return resp

    # ── Frigate Events ─────────────────────────────────────────────────────

    async def list_events(
        self,
        camera: str | None = None,
        label: str | None = None,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> FrigateEventList:
        """Query Frigate detection events with optional filters."""
        data = await self._get(
            "/frigate/events",
            camera=camera,
            label=label,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )
        return _parse_event_list(data)

    async def get_event(self, event_id: str) -> FrigateEvent:
        """Get a single Frigate event by ID."""
        data = await self._get(f"/frigate/events/{event_id}")
        return _parse_event(data)

    # ── Frigate Review Segments ────────────────────────────────────────────

    async def list_review_segments(
        self,
        camera: str | None = None,
        severity: str | None = None,
        reviewed: bool | None = None,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> ReviewSegmentList:
        """Query Frigate review segments with optional filters."""
        data = await self._get(
            "/frigate/reviewsegments",
            camera=camera,
            severity=severity,
            reviewed=reviewed,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )
        return _parse_review_list(data)

    async def get_review_segment(self, review_id: str) -> ReviewSegment:
        """Get a single Frigate review segment by ID."""
        data = await self._get(f"/frigate/reviewsegments/{review_id}")
        return _parse_review(data)

    # ── Video ──────────────────────────────────────────────────────────────

    async def download_clip(self, review_id: str) -> bytes:
        """Download a detection clip as MP4 bytes."""
        return await self._get_bytes(f"/video/clips/{review_id}")

    async def download_clip_to(
        self, review_id: str, path: str | Path,
    ) -> Path:
        """Download a detection clip and save to a file."""
        data = await self.download_clip(review_id)
        dest = Path(path)
        dest.write_bytes(data)
        return dest

    async def list_segments(
        self,
        camera: str,
        start: float | None = None,
        end: float | None = None,
    ) -> SegmentList:
        """List recording segments for a camera and optional time range."""
        data = await self._get(
            "/video/segments",
            camera=camera,
            start=start,
            end=end,
        )
        return _parse_segments(data)

    async def download_video(
        self,
        camera: str,
        start: float,
        end: float,
    ) -> bytes:
        """Download concatenated video for a camera and time range as MP4 bytes."""
        return await self._get_bytes(
            "/video/download",
            camera=camera,
            start=start,
            end=end,
        )

    async def download_video_to(
        self,
        camera: str,
        start: float,
        end: float,
        path: str | Path,
    ) -> Path:
        """Download concatenated video and save to a file."""
        data = await self.download_video(camera, start, end)
        dest = Path(path)
        dest.write_bytes(data)
        return dest


# ── Sync client ────────────────────────────────────────────────────────────────

class VergilClient:
    """Synchronous client for the Vergil station local API.

    Authenticate with a developer token (recommended) — the SDK handles
    the exchange and auto-refresh transparently.  ``station_id`` is
    optional; when omitted the SDK auto-discovers it from ``/health``::

        with VergilClient(
            "http://192.168.1.10:8080",
            galleon_url="https://galleon.example.com",
            developer_token="dev_ey...",
        ) as client:
            health = client.health()

    Or pass a raw station token directly (you manage expiry yourself)::

        with VergilClient("http://192.168.1.10:8080", token="ey...") as client:
            ...
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        galleon_url: str | None = None,
        station_id: str | None = None,
        developer_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._static_token = token
        self._client: httpx.Client | None = None
        self._station_id_resolved = station_id is not None

        if developer_token:
            if not galleon_url:
                raise ValueError(
                    "galleon_url is required when using developer_token"
                )
            self._token_mgr: _SyncTokenManager | None = _SyncTokenManager(
                galleon_url, station_id, developer_token,
            )
        else:
            self._token_mgr = None

    def _resolve_station_id(self) -> None:
        """Fetch station_id from /health if it was not provided."""
        if self._station_id_resolved:
            return
        self._station_id_resolved = True
        data = self._get("/health")
        self._token_mgr.set_station_id(data["id"])

    def _get_auth_headers(self) -> dict[str, str]:
        if self._token_mgr:
            self._resolve_station_id()
            token = self._token_mgr.get_token()
            return {"Authorization": f"Bearer {token}"}
        if self._static_token:
            return {"Authorization": f"Bearer {self._static_token}"}
        return {}

    def _build_client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self._base_url,
            timeout=self._timeout,
        )

    def __enter__(self) -> VergilClient:
        self._client = self._build_client()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._client:
            self._client.close()
            self._client = None

    @property
    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _get(self, path: str, **params: Any) -> dict:
        try:
            resp = self._http.get(
                path,
                params=_strip_none(params),
                headers=self._get_auth_headers(),
            )
        except httpx.ConnectError as e:
            raise VergilConnectionError(str(e)) from e
        return _check(resp)

    def _get_bytes(
        self, path: str, timeout: float | None = None, **params: Any,
    ) -> bytes:
        try:
            resp = self._http.get(
                path,
                params=_strip_none(params),
                headers=self._get_auth_headers(),
                timeout=timeout or VIDEO_TIMEOUT,
            )
        except httpx.ConnectError as e:
            raise VergilConnectionError(str(e)) from e
        if resp.status_code >= 400:
            try:
                body = resp.json()
            except Exception:
                body = {"error": resp.text}
            raise_for_status(resp.status_code, body)
        return resp.content

    def _post(self, path: str, json: dict) -> dict:
        try:
            resp = self._http.post(
                path,
                json=json,
                headers=self._get_auth_headers(),
            )
        except httpx.ConnectError as e:
            raise VergilConnectionError(str(e)) from e
        return _check(resp)

    # ── Health ─────────────────────────────────────────────────────────────

    def health(self) -> HealthStatus:
        """Check station liveness (no auth required)."""
        data = self._get("/health")
        return HealthStatus(status=data["status"], id=data["id"])

    # ── Metrics ────────────────────────────────────────────────────────────

    def get_metrics(self) -> Metrics:
        """Get system metrics (CPU, GPU, RAM, storage, network)."""
        data = self._get("/metrics")
        return Metrics(
            computing=data.get("computing", {}),
            last_report_ts=data.get("last_report_ts"),
        )

    # ── Sensors ────────────────────────────────────────────────────────────

    def get_sensors(self) -> SensorData:
        """Get all sensor telemetry (power and battery)."""
        data = self._get("/sensors")
        return SensorData(
            power=data.get("power", {}),
            battery=data.get("battery", {}),
        )

    def get_power(self) -> dict[str, Any]:
        """Get power sensor data."""
        return self._get("/sensors/power")

    def get_battery(self) -> dict[str, Any]:
        """Get battery sensor data."""
        return self._get("/sensors/battery")

    # ── Crane ──────────────────────────────────────────────────────────────

    def get_crane_status(self) -> CraneStatus:
        """Get current crane status."""
        data = self._get("/crane")
        return CraneStatus(raw=data)

    def send_crane_command(self, command: str) -> CraneCommandResult:
        """Send a crane command: 'crane_up', 'crane_down', or 'crane_stop'."""
        data = self._post("/crane", json={"command": command})
        return CraneCommandResult(command=data["command"])

    # ── Streams ────────────────────────────────────────────────────────────

    def list_streams(self) -> list[Stream]:
        """List all active camera RTSP proxy URIs."""
        data = self._get("/streams")
        return _parse_streams(data)

    def get_stream(self, camera_id: str) -> Stream:
        """Get the RTSP proxy URI for a single camera."""
        data = self._get(f"/streams/{camera_id}")
        return Stream(
            camera_id=data["camera_id"],
            rtsp_uri=data["rtsp_uri"],
            port=data["port"],
        )

    def stream_mic(self) -> httpx.Response:
        """Open a streaming connection to the live mic audio endpoint.

        Returns the raw httpx.Response with ``audio/ogg`` streaming body.
        Caller is responsible for reading and closing the stream::

            resp = client.stream_mic()
            for chunk in resp.iter_bytes():
                process(chunk)
            resp.close()
        """
        try:
            req = self._http.build_request(
                "GET", "/streams/mic",
                headers=self._get_auth_headers(),
            )
            resp = self._http.send(req, stream=True)
        except httpx.ConnectError as e:
            raise VergilConnectionError(str(e)) from e
        if resp.status_code >= 400:
            raise_for_status(resp.status_code, {"error": "mic stream failed"})
        return resp

    # ── Frigate Events ─────────────────────────────────────────────────────

    def list_events(
        self,
        camera: str | None = None,
        label: str | None = None,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> FrigateEventList:
        """Query Frigate detection events with optional filters."""
        data = self._get(
            "/frigate/events",
            camera=camera,
            label=label,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )
        return _parse_event_list(data)

    def get_event(self, event_id: str) -> FrigateEvent:
        """Get a single Frigate event by ID."""
        data = self._get(f"/frigate/events/{event_id}")
        return _parse_event(data)

    # ── Frigate Review Segments ────────────────────────────────────────────

    def list_review_segments(
        self,
        camera: str | None = None,
        severity: str | None = None,
        reviewed: bool | None = None,
        start: float | None = None,
        end: float | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> ReviewSegmentList:
        """Query Frigate review segments with optional filters."""
        data = self._get(
            "/frigate/reviewsegments",
            camera=camera,
            severity=severity,
            reviewed=reviewed,
            start=start,
            end=end,
            limit=limit,
            offset=offset,
        )
        return _parse_review_list(data)

    def get_review_segment(self, review_id: str) -> ReviewSegment:
        """Get a single Frigate review segment by ID."""
        data = self._get(f"/frigate/reviewsegments/{review_id}")
        return _parse_review(data)

    # ── Video ──────────────────────────────────────────────────────────────

    def download_clip(self, review_id: str) -> bytes:
        """Download a detection clip as MP4 bytes."""
        return self._get_bytes(f"/video/clips/{review_id}")

    def download_clip_to(self, review_id: str, path: str | Path) -> Path:
        """Download a detection clip and save to a file."""
        data = self.download_clip(review_id)
        dest = Path(path)
        dest.write_bytes(data)
        return dest

    def list_segments(
        self,
        camera: str,
        start: float | None = None,
        end: float | None = None,
    ) -> SegmentList:
        """List recording segments for a camera and optional time range."""
        data = self._get(
            "/video/segments",
            camera=camera,
            start=start,
            end=end,
        )
        return _parse_segments(data)

    def download_video(
        self,
        camera: str,
        start: float,
        end: float,
    ) -> bytes:
        """Download concatenated video for a camera and time range as MP4 bytes."""
        return self._get_bytes(
            "/video/download",
            camera=camera,
            start=start,
            end=end,
        )

    def download_video_to(
        self,
        camera: str,
        start: float,
        end: float,
        path: str | Path,
    ) -> Path:
        """Download concatenated video and save to a file."""
        data = self.download_video(camera, start, end)
        dest = Path(path)
        dest.write_bytes(data)
        return dest
