"""Write surface for depositing generated drafts into a SharePoint folder.

Deliberately **separate** from :class:`~backend.connectors.graph.connector.GraphConnector`,
which stays read-only. This is the only place the platform writes to M365, and it is
narrow on purpose: it uploads our own generated files (quarterly report drafts) into one
dedicated document-library folder — it never modifies existing SharePoint records.

Auth is the same app-only client-credentials flow the read connector uses, kept
self-contained here so the write path can be reviewed in isolation. Requires the Graph
app to hold a **write** grant on the target site (``Sites.Selected`` write role, or a
broader ``Sites.ReadWrite.All``) — see ``docs/handoffs/sharepoint-write-grant.md``.
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote, urlsplit

from services.http import HttpClient, HttpError
from services.logging import get_logger
from services.secrets import SecretsClient

logger = get_logger(__name__)

_LOGIN_BASE = "https://login.microsoftonline.com"
_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
_EXPIRY_SKEW_SECONDS = 60
_BASE_URL = "https://graph.microsoft.com/v1.0"

# Characters SharePoint/OneDrive disallow in item names (plus control chars/trim).
_ILLEGAL = '"*:<>?/\\|'


def sanitize_name(name: str) -> str:
    """Make a folder/file name safe for SharePoint (illegal chars → space, trimmed)."""
    cleaned = "".join(" " if c in _ILLEGAL else c for c in str(name))
    cleaned = " ".join(cleaned.split())  # collapse whitespace / drop control chars
    return cleaned.strip(" .") or "unnamed"


class SharePointPublisher:
    """Uploads files into a SharePoint site's default document library."""

    def __init__(
        self,
        secrets: SecretsClient,
        http: HttpClient | None = None,
        secret_name: str = "aphrodite/graph",
        base_url: str = _BASE_URL,
    ) -> None:
        self._secrets = secrets
        self._http = http or HttpClient()
        self._secret_name = secret_name
        self._base_url = base_url.rstrip("/")
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # -- auth (app-only client credentials) --------------------------------

    def _fetch_token(self) -> None:
        creds = self._secrets.get(self._secret_name)
        token_url = f"{_LOGIN_BASE}/{creds['tenant_id']}/oauth2/v2.0/token"
        response = self._http.session.post(
            token_url,
            data={
                "grant_type": "client_credentials",
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "scope": _DEFAULT_SCOPE,
            },
            timeout=self._http.timeout,
        )
        if response.status_code >= 400:
            raise HttpError(f"Graph token request failed: {response.status_code}")
        payload = response.json()
        self._token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 3600))
        self._token_expiry = time.monotonic() + expires_in - _EXPIRY_SKEW_SECONDS

    def _ensure_token(self, *, force: bool = False) -> str:
        if force or self._token is None or time.monotonic() >= self._token_expiry:
            self._fetch_token()
        assert self._token is not None
        return self._token

    def _headers(self, *, force: bool = False, content_type: str | None = None) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._ensure_token(force=force)}"}
        if content_type:
            headers["Content-Type"] = content_type
        return headers

    # -- request helper (non-GET verbs, one-shot 401 refresh) --------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Any | None = None,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> Any:
        url = f"{self._base_url}/{path.lstrip('/')}"

        def send(force: bool) -> Any:
            return self._http.session.request(
                method,
                url,
                headers=self._headers(force=force, content_type=content_type),
                json=json,
                data=data,
                timeout=self._http.timeout,
            )

        response = send(force=False)
        if response.status_code == 401:
            response = send(force=True)  # token may be stale; refresh once
        if response.status_code >= 400:
            raise HttpError(f"{method} {path} failed: {response.status_code}")
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    # -- SharePoint operations ---------------------------------------------

    def resolve_site_id(self, site_url: str) -> str:
        """Resolve a site URL (``https://host/sites/Name``) to its Graph site id."""
        parts = urlsplit(site_url)
        site_path = parts.path.lstrip("/")
        site = self._request("GET", f"sites/{quote(parts.netloc)}:/{quote(site_path)}")
        return site["id"]

    def get_item_by_path(self, site_id: str, folder_path: str) -> dict[str, Any]:
        """Return the drive item at ``folder_path`` under the site's default library."""
        path = quote(folder_path.strip("/"), safe="/")
        return self._request("GET", f"sites/{site_id}/drive/root:/{path}")

    def ensure_child_folder(self, site_id: str, parent_item_id: str, name: str) -> dict[str, Any]:
        """Return the child folder ``name`` under ``parent_item_id``, creating it if absent."""
        safe = sanitize_name(name)
        try:
            return self._request(
                "GET", f"sites/{site_id}/drive/items/{parent_item_id}:/{quote(safe)}"
            )
        except HttpError as err:
            if "404" not in str(err):
                raise
        return self._request(
            "POST",
            f"sites/{site_id}/drive/items/{parent_item_id}/children",
            json={
                "name": safe,
                "folder": {},
                "@microsoft.graph.conflictBehavior": "fail",
            },
        )

    def upload_file(
        self,
        site_id: str,
        parent_item_id: str,
        filename: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        """Upload ``content`` as ``filename`` under ``parent_item_id`` (replaces if present).

        Small-file simple upload (the quarterly drafts are well under the 4 MB limit).
        """
        safe = sanitize_name(filename)
        return self._request(
            "PUT",
            f"sites/{site_id}/drive/items/{parent_item_id}:/{quote(safe)}:/content",
            data=content,
            content_type=content_type,
        )
