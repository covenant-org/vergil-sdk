"""Vergil SDK exception hierarchy."""

from __future__ import annotations


class VergilError(Exception):
    """Base exception for all Vergil SDK errors."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class AuthenticationError(VergilError):
    """Raised on 401 — missing, expired, or invalid JWT token."""


class BadRequestError(VergilError):
    """Raised on 400 — invalid parameters or request body."""


class NotFoundError(VergilError):
    """Raised on 404 — requested resource does not exist."""


class ServerError(VergilError):
    """Raised on 5xx — internal station error."""


class ConnectionError(VergilError):
    """Raised when the station is unreachable."""


def raise_for_status(status_code: int, body: dict) -> None:
    """Raise the appropriate VergilError for an error response."""
    message = body.get("error", f"HTTP {status_code}")
    if status_code == 401:
        raise AuthenticationError(message, status_code)
    if status_code == 400:
        raise BadRequestError(message, status_code)
    if status_code == 404:
        raise NotFoundError(message, status_code)
    if status_code >= 500:
        raise ServerError(message, status_code)
    raise VergilError(message, status_code)
