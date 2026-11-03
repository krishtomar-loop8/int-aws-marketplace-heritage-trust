#!/usr/bin/env python3
"""One-time QuickBooks Online OAuth bootstrap: build the authorize URL, then
exchange the returned code for a long-lived refresh token and store it (with the
realm id) in Secrets Manager.

Credentials never pass through the command line or this program's output. Put the
HT-owned production app's ``client_id``/``client_secret`` in a local JSON file
(keep it out of git, e.g. under ``.scratch/``) and delete it afterwards::

    {"client_id": "...", "client_secret": "..."}

Step 1 — generate the link to forward to a customer QuickBooks admin:

    python scripts/quickbooks_oauth_bootstrap.py url --creds .scratch/qbo-creds.json

Step 2 — the admin signs into QuickBooks, selects the HT company, clicks Connect,
and the browser lands on the registered redirect with ``?code=…&realmId=…&state=…``.
Send those back (code expires in ~10 min). Exchange and store:

    python scripts/quickbooks_oauth_bootstrap.py exchange --creds .scratch/qbo-creds.json \\
        --code <CODE> --realm-id <REALM_ID>

Omit ``--code`` to be prompted on stdin (keeps it out of shell history).
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from backend.connectors.quickbooks.oauth_bootstrap import build_authorize_url, exchange_code

DEFAULT_SECRET_ID = "aphrodite/quickbooks"
DEFAULT_REGION = "ca-central-1"
# The registered redirect URI. Uses the SPA origin so no callback endpoint is
# needed — the admin copies code/realmId/state from the browser address bar.
DEFAULT_REDIRECT_URI = "https://d1zumlm52l64zj.cloudfront.net/"


def _load_creds(path: str) -> tuple[str, str]:
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    client_id = data.get("client_id")
    client_secret = data.get("client_secret")
    if not (client_id and client_secret):
        raise SystemExit(f"{path} must contain 'client_id' and 'client_secret'")
    return client_id, client_secret


def _cmd_url(args: argparse.Namespace) -> int:
    client_id, _ = _load_creds(args.creds)
    url, state = build_authorize_url(client_id, redirect_uri=args.redirect_uri)
    print("Send this authorization link to a customer QuickBooks admin:\n")
    print(url)
    print(f"\n(state={state} — Intuit echoes this back with the code and realmId)")
    return 0


def _store_secret(secret: dict[str, Any], *, secret_id: str, region: str) -> None:
    import boto3  # imported lazily so `url` works without AWS configured

    client = boto3.client("secretsmanager", region_name=region)
    client.put_secret_value(SecretId=secret_id, SecretString=json.dumps(secret))


def _cmd_exchange(args: argparse.Namespace) -> int:
    client_id, client_secret = _load_creds(args.creds)
    code = args.code or input("Paste the authorization code from QuickBooks: ").strip()
    if not code:
        raise SystemExit("No authorization code provided")
    if not args.realm_id:
        raise SystemExit("--realm-id is required (Intuit returns realmId on the redirect)")

    payload = exchange_code(
        client_id,
        client_secret,
        code,
        redirect_uri=args.redirect_uri,
    )
    expires_in = payload.get("expires_in", "unknown")
    print(f"Exchange succeeded (access token expires_in={expires_in}s).")

    if args.store:
        secret = {
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": payload["refresh_token"],
            "realm_id": args.realm_id,
            "environment": "production",
        }
        _store_secret(secret, secret_id=args.secret_id, region=args.region)
        print(f"Stored refresh token in Secrets Manager '{args.secret_id}' ({args.region}).")
    else:
        print("--no-store set: refresh token NOT persisted.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QuickBooks one-time OAuth bootstrap")
    parser.add_argument(
        "--redirect-uri", default=DEFAULT_REDIRECT_URI, help="must match the registered URI"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_url = sub.add_parser("url", help="print the authorization URL")
    p_url.add_argument("--creds", required=True, help="JSON file with client_id/client_secret")
    p_url.set_defaults(func=_cmd_url)

    p_ex = sub.add_parser("exchange", help="exchange the code and store the refresh token")
    p_ex.add_argument("--creds", required=True, help="JSON file with client_id/client_secret")
    p_ex.add_argument("--code", help="authorization code (omit to be prompted on stdin)")
    p_ex.add_argument("--realm-id", help="the QuickBooks company id (realmId) from the redirect")
    p_ex.add_argument("--secret-id", default=DEFAULT_SECRET_ID)
    p_ex.add_argument("--region", default=DEFAULT_REGION)
    p_ex.add_argument(
        "--no-store",
        dest="store",
        action="store_false",
        help="exchange but do not write to Secrets Manager",
    )
    p_ex.set_defaults(func=_cmd_exchange, store=True)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
