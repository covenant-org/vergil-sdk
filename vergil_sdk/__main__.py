"""`vergil` CLI — thin wrapper over :mod:`vergil_sdk.oauth`.

Subcommands:
  login    run an OAuth flow and cache credentials
  logout   delete cached credentials
  whoami   decode and print the cached access token claims
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import jwt

from .exceptions import AuthenticationError
from .oauth import (
    default_credentials_path,
    load_credentials,
    login,
    logout,
)


def _resolve_station_id(galleon_url: str, station_id: str | None) -> str:
    if station_id:
        return station_id
    # Station ID is required for OAuth; we don't auto-resolve here because
    # without a token we can't hit the station. The daemon's
    # /.well-known/oauth-protected-resource could help, but we'd need to
    # know the station URL. For now, make the user pass --station-id.
    raise SystemExit("--station-id is required")


def _cmd_login(args: argparse.Namespace) -> int:
    mode = "device" if args.device else "loopback"
    creds_path = Path(args.credentials_path) if args.credentials_path else None
    try:
        creds = login(
            args.galleon_url,
            _resolve_station_id(args.galleon_url, args.station_id),
            mode=mode,
            credentials_path=creds_path,
            client_id=args.client_id,
            open_browser=not args.no_browser,
        )
    except AuthenticationError as e:
        print(f"login failed: {e}", file=sys.stderr)
        return 1
    expires_in = int(creds.expires_at - time.time())
    print(
        f"Logged in to {creds.galleon_url} (station {creds.station_id}). "
        f"Access token expires in {expires_in}s.")
    return 0


def _cmd_logout(args: argparse.Namespace) -> int:
    creds_path = Path(args.credentials_path) if args.credentials_path else None
    removed = logout(
        args.galleon_url,
        _resolve_station_id(args.galleon_url, args.station_id),
        creds_path,
    )
    if removed:
        print("Credentials removed.")
    else:
        print("No credentials found for that station.")
    return 0


def _cmd_whoami(args: argparse.Namespace) -> int:
    creds_path = Path(args.credentials_path) if args.credentials_path else None
    creds = load_credentials(
        args.galleon_url,
        _resolve_station_id(args.galleon_url, args.station_id),
        creds_path,
    )
    if not creds:
        print("No credentials cached.", file=sys.stderr)
        return 1
    try:
        claims = jwt.decode(
            creds.access_token, options={"verify_signature": False})
    except jwt.InvalidTokenError as e:
        print(f"Invalid access token: {e}", file=sys.stderr)
        return 1
    print(json.dumps({
        "galleon_url": creds.galleon_url,
        "station_id": creds.station_id,
        "client_id": creds.client_id,
        "scope": creds.scope,
        "expires_at": creds.expires_at,
        "claims": claims,
    }, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vergil",
        description="Vergil SDK command-line tool.",
    )
    parser.add_argument(
        "--credentials-path",
        default=None,
        help=f"override credentials cache (default: {default_credentials_path()})",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--galleon-url", required=True)
        p.add_argument("--station-id", required=True)

    p_login = sub.add_parser("login", help="Authenticate and cache credentials.")
    add_common(p_login)
    p_login.add_argument("--device", action="store_true",
                         help="Use device authorization grant (headless).")
    p_login.add_argument("--client-id", default=None,
                         help="Skip dynamic registration with this client_id.")
    p_login.add_argument("--no-browser", action="store_true",
                         help="Do not open a browser (loopback mode).")
    p_login.set_defaults(func=_cmd_login)

    p_logout = sub.add_parser("logout", help="Delete cached credentials.")
    add_common(p_logout)
    p_logout.set_defaults(func=_cmd_logout)

    p_whoami = sub.add_parser("whoami", help="Show current cached identity.")
    add_common(p_whoami)
    p_whoami.set_defaults(func=_cmd_whoami)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
