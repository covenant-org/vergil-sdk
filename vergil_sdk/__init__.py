"""Vergil SDK — Python client for the Vergil station local API."""

from .client import AsyncVergilClient, VergilClient
from .exceptions import (
    AuthenticationError,
    BadRequestError,
    ConnectionError,
    NotFoundError,
    ServerError,
    VergilError,
)
from .models import (
    CraneCommandResult,
    CraneStatus,
    FrigateEvent,
    FrigateEventList,
    HealthStatus,
    Metrics,
    MQTTAck,
    MQTTMessage,
    RecordingSegment,
    ReviewSegment,
    ReviewSegmentList,
    SegmentList,
    SensorData,
    Stream,
)
from ._ws import MQTTSubscription

__all__ = [
    # Clients
    "VergilClient",
    "AsyncVergilClient",
    "MQTTSubscription",
    # Models
    "HealthStatus",
    "Metrics",
    "SensorData",
    "CraneStatus",
    "CraneCommandResult",
    "Stream",
    "FrigateEvent",
    "FrigateEventList",
    "ReviewSegment",
    "ReviewSegmentList",
    "RecordingSegment",
    "SegmentList",
    "MQTTMessage",
    "MQTTAck",
    # Exceptions
    "VergilError",
    "AuthenticationError",
    "BadRequestError",
    "NotFoundError",
    "ServerError",
    "ConnectionError",
]
