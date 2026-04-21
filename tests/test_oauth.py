"""Unit tests for the OAuth 2.1 client in vergil_sdk.oauth."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from pathlib import Path

import httpx
import pytest
import respx

from vergil_sdk.exceptions import AuthenticationError
from vergil_sdk.oauth import (
    Credentials,
    OAuthTokenManager,
    _pkce_pair,
    delete_credentials,
    discover,
    load_credentials,
    register_client,
    save_credentials,
)


GALLEON = "https://galleon.test"
STATION = "sta_abc123"
DISCOVERY = {
    "issuer": GALLEON,
    "authorization_endpoint": f"{GALLEON}/api/oauth/authorize",
    "token_endpoint": f"{GALLEON}/api/oauth/token",
    "registration_endpoint": f"{GALLEON}/api/oauth/register",
    "device_authorization_endpoint": f"{GALLEON}/api/oauth/device_authorization",
    "grant_types_supported": [
        "authorization_code",
        "refresh_token",
        "urn:ietf:params:oauth:grant-type:device_code",
    ],
    "code_challenge_methods_supported": ["S256"],
}


# ── PKCE ──────────────────────────────────────────────────────────────────

def test_pkce_pair_matches_s256_spec():
    verifier, challenge = _pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).rstrip(b"=").decode("ascii")
    assert challenge == expected
    assert "=" not in challenge


# ── Discovery ─────────────────────────────────────────────────────────────

@respx.mock
def test_discover_returns_metadata():
    respx.get(f"{GALLEON}/.well-known/oauth-authorization-server").mock(
        return_value=httpx.Response(200, json=DISCOVERY))
    meta = discover(GALLEON)
    assert meta["token_endpoint"] == DISCOVERY["token_endpoint"]


@respx.mock
def test_discover_raises_on_error():
    respx.get(f"{GALLEON}/.well-known/oauth-authorization-server").mock(
        return_value=httpx.Response(500, text="nope"))
    with pytest.raises(AuthenticationError):
        discover(GALLEON)


# ── Dynamic client registration ───────────────────────────────────────────

@respx.mock
def test_register_client_returns_client_id():
    route = respx.post(DISCOVERY["registration_endpoint"]).mock(
        return_value=httpx.Response(201, json={"client_id": "cli_xyz"}))
    cid = register_client(
        DISCOVERY["registration_endpoint"],
        redirect_uris=["http://127.0.0.1/callback"],
    )
    assert cid == "cli_xyz"
    body = json.loads(route.calls[0].request.content)
    assert body["token_endpoint_auth_method"] == "none"
    assert "urn:ietf:params:oauth:grant-type:device_code" in body["grant_types"]
    assert body["redirect_uris"] == ["http://127.0.0.1/callback"]


# ── Credentials store ─────────────────────────────────────────────────────

def _make_creds(expires_at: float = 0.0, refresh: str | None = "r1") -> Credentials:
    return Credentials(
        galleon_url=GALLEON,
        station_id=STATION,
        client_id="cli_xyz",
        access_token="access-123",
        refresh_token=refresh,
        expires_at=expires_at,
        scope="station:full",
    )


def test_credentials_roundtrip(tmp_path: Path):
    path = tmp_path / "creds.json"
    creds = _make_creds(expires_at=time.time() + 3600)
    save_credentials(creds, path=path)
    loaded = load_credentials(GALLEON, STATION, path=path)
    assert loaded == creds
    # File permissions restricted
    assert (path.stat().st_mode & 0o777) == 0o600


def test_credentials_delete(tmp_path: Path):
    path = tmp_path / "creds.json"
    save_credentials(_make_creds(expires_at=time.time() + 3600), path=path)
    assert delete_credentials(GALLEON, STATION, path=path) is True
    assert load_credentials(GALLEON, STATION, path=path) is None
    assert delete_credentials(GALLEON, STATION, path=path) is False


def test_credentials_near_expiry():
    assert _make_creds(expires_at=time.time() - 1).near_expiry() is True
    assert _make_creds(expires_at=time.time() + 3600).near_expiry() is False


# ── OAuthTokenManager ─────────────────────────────────────────────────────

def test_token_manager_raises_when_no_creds(tmp_path: Path):
    mgr = OAuthTokenManager(
        GALLEON, STATION, credentials_path=tmp_path / "creds.json",
    )
    with pytest.raises(AuthenticationError):
        mgr.get_token()


def test_token_manager_returns_cached_access_token(tmp_path: Path):
    path = tmp_path / "creds.json"
    save_credentials(_make_creds(expires_at=time.time() + 3600), path=path)
    mgr = OAuthTokenManager(GALLEON, STATION, credentials_path=path)
    assert mgr.get_token() == "access-123"


@respx.mock
def test_token_manager_refreshes_near_expiry(tmp_path: Path):
    path = tmp_path / "creds.json"
    save_credentials(_make_creds(expires_at=time.time() - 1), path=path)

    respx.get(f"{GALLEON}/.well-known/oauth-authorization-server").mock(
        return_value=httpx.Response(200, json=DISCOVERY))
    respx.post(DISCOVERY["token_endpoint"]).mock(
        return_value=httpx.Response(200, json={
            "access_token": "access-new",
            "refresh_token": "r2",
            "expires_in": 3600,
            "token_type": "Bearer",
        }))

    mgr = OAuthTokenManager(GALLEON, STATION, credentials_path=path)
    assert mgr.get_token() == "access-new"

    # Persisted
    reloaded = load_credentials(GALLEON, STATION, path=path)
    assert reloaded is not None
    assert reloaded.access_token == "access-new"
    assert reloaded.refresh_token == "r2"


@respx.mock
def test_token_manager_refresh_failure_raises(tmp_path: Path):
    path = tmp_path / "creds.json"
    save_credentials(_make_creds(expires_at=time.time() - 1), path=path)

    respx.get(f"{GALLEON}/.well-known/oauth-authorization-server").mock(
        return_value=httpx.Response(200, json=DISCOVERY))
    respx.post(DISCOVERY["token_endpoint"]).mock(
        return_value=httpx.Response(400, json={"error": "invalid_grant"}))

    mgr = OAuthTokenManager(GALLEON, STATION, credentials_path=path)
    with pytest.raises(AuthenticationError):
        mgr.get_token()


@pytest.mark.asyncio
@respx.mock
async def test_token_manager_async_refresh(tmp_path: Path):
    path = tmp_path / "creds.json"
    save_credentials(_make_creds(expires_at=time.time() - 1), path=path)

    respx.get(f"{GALLEON}/.well-known/oauth-authorization-server").mock(
        return_value=httpx.Response(200, json=DISCOVERY))
    respx.post(DISCOVERY["token_endpoint"]).mock(
        return_value=httpx.Response(200, json={
            "access_token": "async-access",
            "refresh_token": "r3",
            "expires_in": 3600,
        }))

    mgr = OAuthTokenManager(GALLEON, STATION, credentials_path=path)
    tok = await mgr.get_token_async()
    assert tok == "async-access"
