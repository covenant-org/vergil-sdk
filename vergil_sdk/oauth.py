"""OAuth 2.1 client for Galleon.

Implements two user-facing grant flows against Galleon's authorization
server:

- **Loopback authorization-code + PKCE** (RFC 8252): opens a browser,
  spins up an ephemeral HTTP server on 127.0.0.1, exchanges the code for
  tokens. Best for desktops/laptops.
- **Device authorization grant** (RFC 8628): prints a URL and user code,
  polls the token endpoint until the user completes consent in another
  browser. Best for headless machines.

Tokens are cached on disk (mode 0600), keyed by
``(galleon_url, station_id)``. :class:`OAuthTokenManager` reads the
cache, transparently refreshes near-expiry access tokens, and — in
interactive mode — re-triggers the login flow when the refresh token
is rejected.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import socket
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Literal

import httpx
import jwt
from platformdirs import user_config_dir

from .exceptions import AuthenticationError

DEFAULT_SCOPE = "station:full"
EXPIRY_SKEW_SECONDS = 60
DEVICE_POLL_DEFAULT_INTERVAL = 5
DEVICE_POLL_MAX_SECONDS = 900
LOOPBACK_TIMEOUT_SECONDS = 300
DISCOVERY_PATH = "/.well-known/oauth-authorization-server"


# ── Data types ────────────────────────────────────────────────────────────

@dataclass
class Credentials:
    galleon_url: str
    station_id: str
    client_id: str
    access_token: str
    refresh_token: str | None
    expires_at: float
    scope: str

    def near_expiry(self, skew: float = EXPIRY_SKEW_SECONDS) -> bool:
        return (self.expires_at - time.time()) <= skew


# ── Credentials file ──────────────────────────────────────────────────────

def default_credentials_path() -> Path:
    return Path(user_config_dir("vergil")) / "credentials.json"


def _cred_key(galleon_url: str, station_id: str) -> str:
    return f"{galleon_url.rstrip('/')}|{station_id}"


def _read_store(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_store(path: Path, store: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(store, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_credentials(
    galleon_url: str, station_id: str, path: Path | None = None,
) -> Credentials | None:
    path = path or default_credentials_path()
    store = _read_store(path)
    raw = store.get(_cred_key(galleon_url, station_id))
    if not raw:
        return None
    try:
        return Credentials(**raw)
    except TypeError:
        return None


def save_credentials(creds: Credentials, path: Path | None = None) -> None:
    path = path or default_credentials_path()
    store = _read_store(path)
    store[_cred_key(creds.galleon_url, creds.station_id)] = asdict(creds)
    _write_store(path, store)


def delete_credentials(
    galleon_url: str, station_id: str, path: Path | None = None,
) -> bool:
    path = path or default_credentials_path()
    store = _read_store(path)
    key = _cred_key(galleon_url, station_id)
    if key not in store:
        return False
    del store[key]
    _write_store(path, store)
    return True


# ── Discovery & registration ──────────────────────────────────────────────

def discover(galleon_url: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
    url = f"{galleon_url.rstrip('/')}{DISCOVERY_PATH}"
    owns = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        resp = client.get(url)
    except httpx.HTTPError as e:
        raise AuthenticationError(f"OAuth discovery failed: {e}") from e
    finally:
        if owns:
            client.close()
    if resp.status_code != 200:
        raise AuthenticationError(
            f"OAuth discovery failed ({resp.status_code}): {resp.text}")
    try:
        return resp.json()
    except ValueError as e:
        raise AuthenticationError(f"OAuth discovery returned invalid JSON: {e}") from e


def register_client(
    registration_endpoint: str,
    *,
    client_name: str = "vergil-sdk",
    redirect_uris: list[str] | None = None,
    client: httpx.Client | None = None,
) -> str:
    owns = client is None
    client = client or httpx.Client(timeout=10.0)
    body: dict[str, Any] = {
        "client_name": client_name,
        "token_endpoint_auth_method": "none",
        "grant_types": [
            "authorization_code",
            "refresh_token",
            "urn:ietf:params:oauth:grant-type:device_code",
        ],
        "response_types": ["code"],
    }
    if redirect_uris:
        body["redirect_uris"] = redirect_uris
    try:
        resp = client.post(registration_endpoint, json=body)
    except httpx.HTTPError as e:
        raise AuthenticationError(f"client registration failed: {e}") from e
    finally:
        if owns:
            client.close()
    if resp.status_code not in (200, 201):
        raise AuthenticationError(
            f"client registration failed ({resp.status_code}): {resp.text}")
    try:
        return resp.json()["client_id"]
    except (ValueError, KeyError) as e:
        raise AuthenticationError(f"unexpected registration response: {e}") from e


# ── PKCE helpers ──────────────────────────────────────────────────────────

def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def _extract_exp(token: str) -> float:
    try:
        claims = jwt.decode(token, options={"verify_signature": False})
        return float(claims.get("exp", 0))
    except jwt.InvalidTokenError:
        return 0.0


def _expires_at(expires_in: Any, fallback_token: str) -> float:
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        return time.time() + float(expires_in)
    return _extract_exp(fallback_token)


# ── Loopback flow ─────────────────────────────────────────────────────────

class _LoopbackHandler(http.server.BaseHTTPRequestHandler):
    server_version = "VergilLoopback/1.0"
    result: dict[str, str] = {}

    def log_message(self, *args: Any, **kwargs: Any) -> None:  # quiet
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.server.result = params  # type: ignore[attr-defined]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            b"<!doctype html><html><body style='font-family:sans-serif'>"
            b"<h2>Vergil login complete</h2>"
            b"<p>You can close this tab and return to your terminal.</p>"
            b"</body></html>"
        )
        self.wfile.write(body)


def _run_loopback_server() -> tuple[http.server.HTTPServer, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = http.server.HTTPServer(("127.0.0.1", port), _LoopbackHandler)
    server.result = None  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def _loopback_login(
    *,
    metadata: dict[str, Any],
    client_id: str,
    station_id: str,
    scope: str,
    open_browser: bool,
    http_client: httpx.Client,
) -> dict[str, Any]:
    auth_endpoint = metadata["authorization_endpoint"]
    token_endpoint = metadata["token_endpoint"]

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)

    server, port = _run_loopback_server()
    redirect_uri = f"http://127.0.0.1:{port}/callback"

    try:
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "scope": scope,
            "station_id": station_id,
            "state": state,
        }
        auth_url = f"{auth_endpoint}?{urllib.parse.urlencode(params)}"

        if open_browser:
            webbrowser.open(auth_url)
        print(f"Open this URL to sign in:\n  {auth_url}", flush=True)

        deadline = time.time() + LOOPBACK_TIMEOUT_SECONDS
        while time.time() < deadline:
            if getattr(server, "result", None):
                break
            time.sleep(0.1)
        result: dict[str, str] | None = getattr(server, "result", None)
        if not result:
            raise AuthenticationError("loopback login timed out")

        if "error" in result:
            raise AuthenticationError(
                f"authorization failed: {result.get('error')} "
                f"{result.get('error_description', '')}".strip())
        if result.get("state") != state:
            raise AuthenticationError("state mismatch in loopback callback")
        code = result.get("code")
        if not code:
            raise AuthenticationError("no authorization code returned")
    finally:
        server.shutdown()
        server.server_close()

    resp = http_client.post(
        token_endpoint,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "code_verifier": verifier,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise AuthenticationError(
            f"token exchange failed ({resp.status_code}): {resp.text}")
    return resp.json()


# ── Device flow ───────────────────────────────────────────────────────────

def _device_login(
    *,
    metadata: dict[str, Any],
    client_id: str,
    station_id: str,
    scope: str,
    http_client: httpx.Client,
    print_fn: Callable[[str], None] = print,
) -> dict[str, Any]:
    device_endpoint = metadata.get("device_authorization_endpoint")
    if not device_endpoint:
        raise AuthenticationError(
            "Galleon does not advertise a device_authorization_endpoint")
    token_endpoint = metadata["token_endpoint"]

    resp = http_client.post(
        device_endpoint,
        data={
            "client_id": client_id,
            "scope": scope,
            "station_id": station_id,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    if resp.status_code != 200:
        raise AuthenticationError(
            f"device authorization failed ({resp.status_code}): {resp.text}")
    dev = resp.json()
    device_code = dev["device_code"]
    user_code = dev["user_code"]
    verification_uri = dev["verification_uri"]
    verification_uri_complete = dev.get("verification_uri_complete")
    interval = int(dev.get("interval", DEVICE_POLL_DEFAULT_INTERVAL))
    expires_in = int(dev.get("expires_in", DEVICE_POLL_MAX_SECONDS))

    if verification_uri_complete:
        print_fn(
            f"To complete sign-in, visit:\n  {verification_uri_complete}\n"
            f"Or go to {verification_uri} and enter code: {user_code}")
    else:
        print_fn(
            f"To complete sign-in, visit:\n  {verification_uri}\n"
            f"and enter code: {user_code}")

    deadline = time.time() + expires_in
    while time.time() < deadline:
        time.sleep(interval)
        poll = http_client.post(
            token_endpoint,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": device_code,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if poll.status_code == 200:
            return poll.json()
        try:
            body = poll.json()
        except ValueError:
            body = {}
        err = body.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval += 5
            continue
        if err in ("expired_token", "access_denied"):
            raise AuthenticationError(f"device flow aborted: {err}")
        raise AuthenticationError(
            f"device flow failed ({poll.status_code}): {poll.text}")

    raise AuthenticationError("device flow timed out")


# ── Public API ────────────────────────────────────────────────────────────

def login(
    galleon_url: str,
    station_id: str,
    *,
    mode: Literal["loopback", "device"] = "loopback",
    credentials_path: Path | None = None,
    client_id: str | None = None,
    scope: str = DEFAULT_SCOPE,
    open_browser: bool = True,
) -> Credentials:
    """Run an OAuth flow and persist the resulting credentials.

    ``mode="loopback"`` opens a browser and handles the callback on
    127.0.0.1; ``mode="device"`` prints a URL + user code.
    """
    galleon_url = galleon_url.rstrip("/")
    with httpx.Client(timeout=30.0) as http_client:
        metadata = discover(galleon_url, client=http_client)

        if client_id is None:
            existing = load_credentials(galleon_url, station_id, credentials_path)
            if existing and existing.client_id:
                client_id = existing.client_id
            else:
                reg = metadata.get("registration_endpoint")
                if not reg:
                    raise AuthenticationError(
                        "no client_id supplied and AS has no registration_endpoint")
                # Register a loopback placeholder; the authorization
                # server matches any 127.0.0.1 port per RFC 8252 §7.3.
                client_id = register_client(
                    reg, client=http_client,
                    redirect_uris=["http://127.0.0.1/callback"],
                )

        if mode == "loopback":
            token = _loopback_login(
                metadata=metadata,
                client_id=client_id,
                station_id=station_id,
                scope=scope,
                open_browser=open_browser,
                http_client=http_client,
            )
        elif mode == "device":
            token = _device_login(
                metadata=metadata,
                client_id=client_id,
                station_id=station_id,
                scope=scope,
                http_client=http_client,
            )
        else:
            raise ValueError(f"unknown login mode: {mode}")

    access_token = token["access_token"]
    creds = Credentials(
        galleon_url=galleon_url,
        station_id=station_id,
        client_id=client_id,
        access_token=access_token,
        refresh_token=token.get("refresh_token"),
        expires_at=_expires_at(token.get("expires_in"), access_token),
        scope=token.get("scope", scope),
    )
    save_credentials(creds, credentials_path)
    return creds


def logout(
    galleon_url: str,
    station_id: str,
    credentials_path: Path | None = None,
) -> bool:
    """Delete cached credentials. Returns True if an entry was removed."""
    return delete_credentials(galleon_url.rstrip("/"), station_id, credentials_path)


# ── Token manager ─────────────────────────────────────────────────────────

class OAuthTokenManager:
    """Reads cached OAuth credentials and refreshes them on demand.

    Works for both sync and async callers (``get_token`` /
    ``get_token_async``). When ``interactive=True`` and no cached
    credential exists (or the refresh token is rejected), the loopback
    login flow is re-run transparently.
    """

    def __init__(
        self,
        galleon_url: str,
        station_id: str,
        *,
        credentials_path: Path | None = None,
        interactive: bool = False,
        scope: str = DEFAULT_SCOPE,
    ) -> None:
        self._galleon_url = galleon_url.rstrip("/")
        self._station_id = station_id
        self._credentials_path = credentials_path
        self._interactive = interactive
        self._scope = scope
        self._creds: Credentials | None = load_credentials(
            self._galleon_url, station_id, credentials_path,
        )
        self._metadata: dict[str, Any] | None = None

    def set_station_id(self, station_id: str) -> None:
        if station_id == self._station_id:
            return
        self._station_id = station_id
        self._creds = load_credentials(
            self._galleon_url, station_id, self._credentials_path,
        )

    def _missing_creds_error(self) -> AuthenticationError:
        return AuthenticationError(
            "no credentials — run `vergil login "
            f"--galleon-url {self._galleon_url} --station-id {self._station_id}`")

    def _ensure_creds(self) -> Credentials:
        if self._creds is None:
            if self._interactive:
                self._creds = login(
                    self._galleon_url, self._station_id,
                    credentials_path=self._credentials_path,
                    scope=self._scope,
                )
            else:
                raise self._missing_creds_error()
        return self._creds

    def _refresh_sync(self, creds: Credentials, http_client: httpx.Client) -> Credentials:
        if not creds.refresh_token:
            if self._interactive:
                self._creds = login(
                    self._galleon_url, self._station_id,
                    credentials_path=self._credentials_path,
                    client_id=creds.client_id,
                    scope=self._scope,
                )
                return self._creds
            raise self._missing_creds_error()

        if self._metadata is None:
            self._metadata = discover(self._galleon_url, client=http_client)
        token_endpoint = self._metadata["token_endpoint"]

        resp = http_client.post(
            token_endpoint,
            data={
                "grant_type": "refresh_token",
                "refresh_token": creds.refresh_token,
                "client_id": creds.client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if resp.status_code != 200:
            if self._interactive and resp.status_code in (400, 401):
                self._creds = login(
                    self._galleon_url, self._station_id,
                    credentials_path=self._credentials_path,
                    client_id=creds.client_id,
                    scope=self._scope,
                )
                return self._creds
            raise AuthenticationError(
                f"token refresh failed ({resp.status_code}): {resp.text}. "
                "Run `vergil login` to re-authenticate.")
        data = resp.json()
        access = data["access_token"]
        new = Credentials(
            galleon_url=creds.galleon_url,
            station_id=creds.station_id,
            client_id=creds.client_id,
            access_token=access,
            refresh_token=data.get("refresh_token", creds.refresh_token),
            expires_at=_expires_at(data.get("expires_in"), access),
            scope=data.get("scope", creds.scope),
        )
        save_credentials(new, self._credentials_path)
        self._creds = new
        return new

    def get_token(self) -> str:
        creds = self._ensure_creds()
        if creds.near_expiry():
            with httpx.Client(timeout=10.0) as client:
                creds = self._refresh_sync(creds, client)
        return creds.access_token

    async def get_token_async(self) -> str:
        creds = self._ensure_creds()
        if not creds.near_expiry():
            return creds.access_token
        if not creds.refresh_token:
            return self.get_token()  # falls through to interactive/error

        if self._metadata is None:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    f"{self._galleon_url}{DISCOVERY_PATH}")
            if resp.status_code != 200:
                raise AuthenticationError(
                    f"OAuth discovery failed ({resp.status_code}): {resp.text}")
            self._metadata = resp.json()
        token_endpoint = self._metadata["token_endpoint"]

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                token_endpoint,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": creds.refresh_token,
                    "client_id": creds.client_id,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        if resp.status_code != 200:
            if self._interactive and resp.status_code in (400, 401):
                self._creds = login(
                    self._galleon_url, self._station_id,
                    credentials_path=self._credentials_path,
                    client_id=creds.client_id,
                    scope=self._scope,
                )
                return self._creds.access_token
            raise AuthenticationError(
                f"token refresh failed ({resp.status_code}): {resp.text}. "
                "Run `vergil login` to re-authenticate.")
        data = resp.json()
        access = data["access_token"]
        self._creds = Credentials(
            galleon_url=creds.galleon_url,
            station_id=creds.station_id,
            client_id=creds.client_id,
            access_token=access,
            refresh_token=data.get("refresh_token", creds.refresh_token),
            expires_at=_expires_at(data.get("expires_in"), access),
            scope=data.get("scope", creds.scope),
        )
        save_credentials(self._creds, self._credentials_path)
        return access
