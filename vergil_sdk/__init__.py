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
    SirenCommandResult,
    SirenStatus,
    SpeakerPlayResult,
    Stream,
)
from ._ws import MicWebmStream, MQTTSubscription, SpeakerMicSession
from .oauth import Credentials, OAuthTokenManager, login, logout

__all__ = [
    # Clients
    "VergilClient",
    "AsyncVergilClient",
    "MQTTSubscription",
    "SpeakerMicSession",
    "MicWebmStream",
    # OAuth
    "login",
    "logout",
    "Credentials",
    "OAuthTokenManager",
    # Models
    "HealthStatus",
    "Metrics",
    "SensorData",
    "CraneStatus",
    "CraneCommandResult",
    "Stream",
    "SirenStatus",
    "SirenCommandResult",
    "SpeakerPlayResult",
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
