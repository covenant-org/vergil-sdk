"""Response models for the Vergil station API."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── Health ─────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class HealthStatus:
    status: str
    id: str


# ── Metrics ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Metrics:
    computing: dict[str, Any]
    last_report_ts: str | None


# ── Sensors ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SensorData:
    power: dict[str, Any]
    battery: dict[str, Any]


# ── Crane ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CraneStatus:
    raw: dict[str, Any]

    @property
    def status(self) -> str:
        return self.raw.get("status", "unknown")


@dataclass(frozen=True)
class CraneCommandResult:
    command: str


# ── Streams ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Stream:
    camera_id: str
    rtsp_uri: str
    port: int


# ── Frigate Events ─────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FrigateEvent:
    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return self.raw.get("id", "")

    @property
    def camera(self) -> str:
        return self.raw.get("camera", "")

    @property
    def label(self) -> str:
        return self.raw.get("label", "")

    @property
    def start_time(self) -> float | None:
        return self.raw.get("start_time")

    @property
    def end_time(self) -> float | None:
        return self.raw.get("end_time")

    @property
    def zones(self) -> list[str]:
        return self.raw.get("zones", [])

    @property
    def data(self) -> dict[str, Any]:
        return self.raw.get("data", {})


@dataclass(frozen=True)
class FrigateEventList:
    events: list[FrigateEvent]
    count: int


# ── Frigate Review Segments ───────────────────────────────────────────────────

@dataclass(frozen=True)
class ReviewSegment:
    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return self.raw.get("id", "")

    @property
    def camera(self) -> str:
        return self.raw.get("camera", "")

    @property
    def start_time(self) -> float | None:
        return self.raw.get("start_time")

    @property
    def end_time(self) -> float | None:
        return self.raw.get("end_time")

    @property
    def severity(self) -> str:
        return self.raw.get("severity", "")

    @property
    def has_been_reviewed(self) -> bool:
        return bool(self.raw.get("has_been_reviewed"))

    @property
    def data(self) -> dict[str, Any]:
        return self.raw.get("data", {})


@dataclass(frozen=True)
class ReviewSegmentList:
    reviewsegments: list[ReviewSegment]
    count: int


# ── Video ──────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RecordingSegment:
    camera: str
    start_time: float
    end_time: float
    duration: float
    path: str


@dataclass(frozen=True)
class SegmentList:
    segments: list[RecordingSegment]
    count: int


# ── WebSocket / MQTT ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class MQTTMessage:
    topic: str
    payload: Any
    timestamp: str


@dataclass(frozen=True)
class MQTTAck:
    action: str  # "subscribed" or "unsubscribed"
    topics: list[str]
