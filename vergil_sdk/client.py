"""Synchronous and asynchronous HTTP clients for the Vergil station API."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from .exceptions import ConnectionError as VergilConnectionError
from .exceptions import raise_for_status
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
    SirenCommandResult,
    SirenStatus,
    SpeakerPlayResult,
    Stream,
)
from collections.abc import AsyncIterable, Iterable
from .oauth import OAuthTokenManager

DEFAULT_TIMEOUT = 30.0
VIDEO_TIMEOUT = 120.0

unrestricted_paths = ["/health"]


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


# ── Async client ───────────────────────────────────────────────────────────────

class AsyncVergilClient:
    """Async client for the Vergil station local API.

    Two auth modes:

    1. **OAuth** — pass ``galleon_url`` + ``station_id``; the SDK loads
       credentials cached by ``vergil login`` and silently refreshes
       them. ``station_id`` is optional if auto-discovery is acceptable,
       but is recommended because the credentials cache is keyed by
       ``(galleon_url, station_id)``::

            async with AsyncVergilClient(
                "http://192.168.1.10:8082",
                galleon_url="https://galleon.example.com",
                station_id="sta_abc123",
            ) as client:
                health = await client.health()

    2. **Raw token** — pass ``token`` if you already hold a station JWT;
       you manage expiry yourself::

            async with AsyncVergilClient(
                "http://192.168.1.10:8082", token="ey...",
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
        credentials_path: Path | None = None,
        interactive: bool = False,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._static_token = token
        self._client: httpx.AsyncClient | None = None
        self._station_id_resolved = station_id is not None

        if galleon_url:
            self._token_mgr: OAuthTokenManager | None = OAuthTokenManager(
                galleon_url,
                station_id or "",
                credentials_path=credentials_path,
                interactive=interactive,
            )
        else:
            self._token_mgr = None

    async def _resolve_station_id(self) -> None:
        if self._station_id_resolved or self._token_mgr is None:
            return
        self._station_id_resolved = True
        data = await self._get("/health")
        self._token_mgr.set_station_id(data["id"])

    async def _get_auth_headers(self) -> dict[str, str]:
        if self._token_mgr:
            await self._resolve_station_id()
            token = await self._token_mgr.get_token_async()
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
            headers = {}
            if path not in unrestricted_paths:
                headers = await self._get_auth_headers()

            resp = await self._http.get(
                path,
                params=_strip_none(params),
                headers=headers,
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

    # ── Siren ──────────────────────────────────────────────────────────────

    async def get_siren(self) -> SirenStatus:
        """Get latest cambox siren/light status and last error."""
        data = await self._get("/siren")
        return SirenStatus(
            status=data.get("status") or {},
            error=data.get("error"),
        )

    async def set_siren_light(
        self, mode: str, state: bool,
    ) -> SirenCommandResult:
        """Set the cambox light: mode in {'slow','fast','strobe'}."""
        data = await self._post(
            "/siren/light", json={"mode": mode, "state": state},
        )
        return SirenCommandResult(
            device=data["device"], mode=data["mode"], state=data["state"],
        )

    async def set_siren_megaphone(
        self, mode: str, state: bool,
    ) -> SirenCommandResult:
        """Set the cambox megaphone: mode in {'continue','police','ambulance'}."""
        data = await self._post(
            "/siren/megaphone", json={"mode": mode, "state": state},
        )
        return SirenCommandResult(
            device=data["device"], mode=data["mode"], state=data["state"],
        )

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

    # ── Speakers ───────────────────────────────────────────────────────────

    async def play_audio(
        self,
        data: bytes | str | Path,
        *,
        content_type: str | None = None,
        filename: str | None = None,
    ) -> SpeakerPlayResult:
        """Play a complete audio file on the host speakers.

        ``data`` may be a path to a local file or raw bytes. When a path is
        given, the filename is forwarded as a multipart upload so the server
        can infer the container type. When bytes are given, supply
        ``content_type`` (e.g. ``"audio/wav"``) so the server can decode it.
        """
        path: Path | None = None
        if isinstance(data, (str, Path)):
            path = Path(data)
            payload = path.read_bytes()
            send_name = filename or path.name
        else:
            payload = data
            send_name = filename

        try:
            if send_name:
                files = {
                    "file": (
                        send_name,
                        payload,
                        content_type or "application/octet-stream",
                    )
                }
                resp = await self._http.post(
                    "/speakers/play",
                    files=files,
                    headers=await self._get_auth_headers(),
                )
            else:
                headers = await self._get_auth_headers()
                if content_type:
                    headers["Content-Type"] = content_type
                resp = await self._http.post(
                    "/speakers/play",
                    content=payload,
                    headers=headers,
                )
        except httpx.ConnectError as e:
            raise VergilConnectionError(str(e)) from e
        body = _check(resp)
        return SpeakerPlayResult(
            status=body.get("status", ""),
            bytes=body.get("bytes", 0),
        )

    async def stream_audio(
        self,
        chunks: AsyncIterable[bytes] | Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
    ) -> SpeakerPlayResult:
        """Stream audio bytes to the host speakers via HTTP/1.1 chunked upload.

        ``chunks`` is consumed and forwarded as the request body. Returns once
        the server finishes playback (or the upload is closed).
        """
        headers = await self._get_auth_headers()
        headers["Content-Type"] = content_type
        try:
            resp = await self._http.post(
                "/speakers/stream",
                content=chunks,
                headers=headers,
                timeout=None,
            )
        except httpx.ConnectError as e:
            raise VergilConnectionError(str(e)) from e
        body = _check(resp)
        return SpeakerPlayResult(
            status=body.get("status", ""),
            bytes=body.get("bytes", 0),
        )

    def speakers_mic_page_url(self, token: str) -> str:
        """Build the URL for the browser ``/speakers/mic`` page."""
        return f"{self._base_url}/speakers/mic?token={token}"

    def speakers_duplex_page_url(self, token: str) -> str:
        """Build the URL for the browser ``/speakers/duplex`` page."""
        return f"{self._base_url}/speakers/duplex?token={token}"

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

    See :class:`AsyncVergilClient` for the auth modes.
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
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._static_token = token
        self._client: httpx.Client | None = None
        self._station_id_resolved = station_id is not None

        if galleon_url:
            self._token_mgr: OAuthTokenManager | None = OAuthTokenManager(
                galleon_url,
                station_id or "",
                credentials_path=credentials_path,
                interactive=interactive,
            )
        else:
            self._token_mgr = None

    def _resolve_station_id(self) -> None:
        if self._station_id_resolved or self._token_mgr is None:
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
            headers = {}
            if path not in unrestricted_paths:
                headers = self._get_auth_headers()
            resp = self._http.get(
                path,
                params=_strip_none(params),
                headers=headers,
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

    # ── Siren ──────────────────────────────────────────────────────────────

    def get_siren(self) -> SirenStatus:
        """Get latest cambox siren/light status and last error."""
        data = self._get("/siren")
        return SirenStatus(
            status=data.get("status") or {},
            error=data.get("error"),
        )

    def set_siren_light(self, mode: str, state: bool) -> SirenCommandResult:
        """Set the cambox light: mode in {'slow','fast','strobe'}."""
        data = self._post(
            "/siren/light", json={"mode": mode, "state": state},
        )
        return SirenCommandResult(
            device=data["device"], mode=data["mode"], state=data["state"],
        )

    def set_siren_megaphone(
        self, mode: str, state: bool,
    ) -> SirenCommandResult:
        """Set the cambox megaphone: mode in {'continue','police','ambulance'}."""
        data = self._post(
            "/siren/megaphone", json={"mode": mode, "state": state},
        )
        return SirenCommandResult(
            device=data["device"], mode=data["mode"], state=data["state"],
        )

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

    # ── Speakers ───────────────────────────────────────────────────────────

    def play_audio(
        self,
        data: bytes | str | Path,
        *,
        content_type: str | None = None,
        filename: str | None = None,
    ) -> SpeakerPlayResult:
        """Play a complete audio file on the host speakers.

        ``data`` may be a path to a local file or raw bytes. Pass
        ``content_type`` (e.g. ``"audio/wav"``) when uploading raw bytes.
        """
        path: Path | None = None
        if isinstance(data, (str, Path)):
            path = Path(data)
            payload = path.read_bytes()
            send_name = filename or path.name
        else:
            payload = data
            send_name = filename

        try:
            if send_name:
                files = {
                    "file": (
                        send_name,
                        payload,
                        content_type or "application/octet-stream",
                    )
                }
                resp = self._http.post(
                    "/speakers/play",
                    files=files,
                    headers=self._get_auth_headers(),
                )
            else:
                headers = self._get_auth_headers()
                if content_type:
                    headers["Content-Type"] = content_type
                resp = self._http.post(
                    "/speakers/play",
                    content=payload,
                    headers=headers,
                )
        except httpx.ConnectError as e:
            raise VergilConnectionError(str(e)) from e
        body = _check(resp)
        return SpeakerPlayResult(
            status=body.get("status", ""),
            bytes=body.get("bytes", 0),
        )

    def stream_audio(
        self,
        chunks: Iterable[bytes],
        *,
        content_type: str = "application/octet-stream",
    ) -> SpeakerPlayResult:
        """Stream audio bytes to the host speakers via HTTP/1.1 chunked upload."""
        headers = self._get_auth_headers()
        headers["Content-Type"] = content_type
        try:
            resp = self._http.post(
                "/speakers/stream",
                content=chunks,
                headers=headers,
                timeout=None,
            )
        except httpx.ConnectError as e:
            raise VergilConnectionError(str(e)) from e
        body = _check(resp)
        return SpeakerPlayResult(
            status=body.get("status", ""),
            bytes=body.get("bytes", 0),
        )

    def speakers_mic_page_url(self, token: str) -> str:
        """Build the URL for the browser ``/speakers/mic`` page."""
        return f"{self._base_url}/speakers/mic?token={token}"

    def speakers_duplex_page_url(self, token: str) -> str:
        """Build the URL for the browser ``/speakers/duplex`` page."""
        return f"{self._base_url}/speakers/duplex?token={token}"

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
