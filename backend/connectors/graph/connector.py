"""Read-only Microsoft Graph connector.

App-only (client-credentials) access to Microsoft Graph v1.0. The connector
reads SharePoint document libraries, Outlook mail, calendar events, and the
directory in support of the global matter-search spec.

Read-only by design: every data method issues a GET. The single POST is the
OAuth token request. The directory/content search helper uses Graph's
``POST /search/query`` endpoint, which despite the HTTP verb is a read-only
query (it returns results and mutates nothing); it is the only supported way to
run a Graph search and is documented as such here.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

from services.http import HttpClient, HttpError
from services.logging import get_logger
from services.secrets import SecretsClient

logger = get_logger(__name__)

_LOGIN_BASE = "https://login.microsoftonline.com"
_DEFAULT_SCOPE = "https://graph.microsoft.com/.default"
# Refresh the app token slightly before it actually expires.
_EXPIRY_SKEW_SECONDS = 60


class GraphConnector:
    """Read-only Microsoft Graph client (SharePoint/M365 + Outlook).

    Authentication is lazy: the app-only token is fetched on first use and
    cached until shortly before expiry, then refreshed. A 401 also triggers a
    one-time forced refresh and retry.
    """

    def __init__(
        self,
        secrets: SecretsClient,
        http: HttpClient | None = None,
        secret_name: str = "aphrodite/graph",
        base_url: str = "https://graph.microsoft.com/v1.0",
    ) -> None:
        self._secrets = secrets
        self._http = http or HttpClient()
        self._secret_name = secret_name
        self._base_url = base_url.rstrip("/")
        self._token: str | None = None
        self._token_expiry: float = 0.0

    # -- authentication -------------------------------------------------

    def _credentials(self, *, refresh: bool = False) -> dict[str, Any]:
        return self._secrets.get(self._secret_name, refresh=refresh)

    def _fetch_token(self, *, refresh_secret: bool = False) -> None:
        creds = self._credentials(refresh=refresh_secret)
        tenant_id = creds["tenant_id"]
        token_url = f"{_LOGIN_BASE}/{tenant_id}/oauth2/v2.0/token"
        data = {
            "grant_type": "client_credentials",
            "client_id": creds["client_id"],
            "client_secret": creds["client_secret"],
            "scope": _DEFAULT_SCOPE,
        }
        # OAuth token endpoint is the only POST; it returns a bearer token.
        response = self._http.session.post(token_url, data=data, timeout=self._http.timeout)
        if response.status_code >= 400:
            raise HttpError(f"Graph token request failed: {response.status_code}")
        payload = response.json()
        self._token = payload["access_token"]
        expires_in = float(payload.get("expires_in", 3600))
        self._token_expiry = time.monotonic() + expires_in - _EXPIRY_SKEW_SECONDS
        logger.info("graph token acquired", extra={"extra_fields": {"expires_in": expires_in}})

    def _ensure_token(self, *, force: bool = False) -> str:
        if force or self._token is None or time.monotonic() >= self._token_expiry:
            self._fetch_token(refresh_secret=force)
        assert self._token is not None
        return self._token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._ensure_token()}"}

    # -- request helpers ------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _get_json(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        url = self._url(path)

        def headers() -> dict[str, str]:
            h = self._headers()
            if extra_headers:
                h.update(extra_headers)
            return h

        try:
            return self._http.get_json(url, headers=headers(), params=params)
        except HttpError as err:
            if "401" in str(err):
                # Token may have been revoked/rotated; force-refresh once.
                self._ensure_token(force=True)
                return self._http.get_json(url, headers=headers(), params=params)
            raise

    def _paginate(self, path: str, *, params: dict[str, Any] | None = None) -> Iterator[Any]:
        url = self._url(path)
        # Refresh the token up front so the paginator reuses one set of headers.
        headers = self._headers()
        try:
            yield from self._http.paginate(
                url,
                items=lambda d: d.get("value", []),
                next_url=lambda d: d.get("@odata.nextLink"),
                headers=headers,
                params=params,
            )
        except HttpError as err:
            if "401" in str(err):
                headers = {"Authorization": f"Bearer {self._ensure_token(force=True)}"}
                yield from self._http.paginate(
                    url,
                    items=lambda d: d.get("value", []),
                    next_url=lambda d: d.get("@odata.nextLink"),
                    headers=headers,
                    params=params,
                )
            else:
                raise

    # -- directory ------------------------------------------------------

    def get_user(self, user_id: str) -> dict[str, Any]:
        """Return a single user (mailbox/calendar owner or correspondent)."""
        return self._get_json(f"users/{quote(user_id)}")

    def list_users(self, *, search: str | None = None, top: int | None = None) -> list[Any]:
        """List directory users, optionally filtered by a ``$search`` term.

        Graph requires the ``ConsistencyLevel: eventual`` header for ``$search``
        on the users collection. ``$select`` pulls the fields the mailbox fan-out
        needs to skip non-searchable (unlicensed / disabled / guest) accounts.
        """
        params: dict[str, Any] = {
            "$select": "id,displayName,mail,accountEnabled,assignedLicenses,userType",
        }
        if top is not None:
            params["$top"] = top
        if search is not None:
            params["$search"] = f'"displayName:{search}"'
            return list(self._paginate_with_consistency("users", params=params))
        return list(self._paginate("users", params=params))

    def _paginate_with_consistency(
        self, path: str, *, params: dict[str, Any] | None = None
    ) -> Iterator[Any]:
        url = self._url(path)
        headers = self._headers()
        headers["ConsistencyLevel"] = "eventual"
        yield from self._http.paginate(
            url,
            items=lambda d: d.get("value", []),
            next_url=lambda d: d.get("@odata.nextLink"),
            headers=headers,
            params=params,
        )

    # -- mail (correspondence) -----------------------------------------

    def list_messages(
        self,
        user_id: str,
        *,
        search: str | None = None,
        filter: str | None = None,
        top: int | None = None,
    ) -> list[Any]:
        """List a mailbox's messages, optionally via ``$search``/``$filter``.

        ``$search`` and ``$filter`` are mutually exclusive in Graph mail queries;
        ``$search`` takes precedence if both are supplied.
        """
        params: dict[str, Any] = {}
        if top is not None:
            params["$top"] = top
        if search is not None:
            params["$search"] = f'"{search}"'
        elif filter is not None:
            params["$filter"] = filter
        return list(self._paginate(f"users/{quote(user_id)}/messages", params=params or None))

    def search_mailbox(self, user_id: str, kql: str, *, top: int = 25) -> list[Any]:
        """Search one mailbox with a KQL query, returning a single bounded page.

        Used by the matter-search fan-out to find matter correspondence across
        mailboxes. Pass a bare-term KQL query (e.g. ``McDonald AND Estate``) to
        search subject, body and attachment text; Graph returns these
        relevance-ranked. The fan-out requires every token (``AND``) and bounds
        volume via ``top`` + a global cap. Unlike :meth:`list_messages` this does
        **not** paginate: it returns at most ``top`` most-relevant messages, so a
        broad fan-out over many mailboxes stays bounded.
        """
        params = {
            "$search": f'"{kql}"',
            "$top": top,
            # internetMessageId (RFC id, identical across every recipient's copy)
            # and conversationId drive the fan-out's cross-mailbox dedup and
            # per-thread capping; the body itself is hydrated separately for the
            # few selected messages (get_message), not fetched per search hit.
            # toRecipients/ccRecipients let the fan-out drop messages exchanged
            # with excluded correspondents (e.g. the dev team's @loop8.io address).
            "$select": (
                "id,subject,from,toRecipients,ccRecipients,receivedDateTime,"
                "bodyPreview,webLink,internetMessageId,conversationId"
            ),
        }
        payload = self._get_json(f"users/{quote(user_id)}/messages", params=params)
        value = payload.get("value", [])
        return value if isinstance(value, list) else []

    def get_message(
        self,
        user_id: str,
        message_id: str,
        *,
        select: str | None = None,
        body_as_text: bool = False,
    ) -> dict[str, Any]:
        """Return a single mail message.

        ``select`` limits the fields fetched. ``body_as_text`` asks Graph to
        convert HTML bodies to plain text server-side
        (``Prefer: outlook.body-content-type="text"``) — used when hydrating full
        message bodies for the dossier.
        """
        params = {"$select": select} if select else None
        extra = {"Prefer": 'outlook.body-content-type="text"'} if body_as_text else None
        return self._get_json(
            f"users/{quote(user_id)}/messages/{quote(message_id)}",
            params=params,
            extra_headers=extra,
        )

    # -- calendar ------------------------------------------------------

    def list_calendar_events(self, user_id: str, *, top: int | None = None) -> list[Any]:
        """List a user's calendar events (matter deadlines / diarized dates)."""
        params: dict[str, Any] = {}
        if top is not None:
            params["$top"] = top
        return list(self._paginate(f"users/{quote(user_id)}/events", params=params or None))

    def list_calendar_view(
        self, user_id: str, *, start: str, end: str, top: int | None = None
    ) -> list[Any]:
        """List events within a date window via ``/calendarView``.

        ``start``/``end`` are ISO-8601 datetimes (``startDateTime``/``endDateTime``).
        """
        params: dict[str, Any] = {"startDateTime": start, "endDateTime": end}
        if top is not None:
            params["$top"] = top
        return list(self._paginate(f"users/{quote(user_id)}/calendarView", params=params))

    # -- SharePoint (documents) ----------------------------------------

    def get_site(self, hostname: str, path: str) -> dict[str, Any]:
        """Resolve a SharePoint site by hostname and server-relative path.

        e.g. ``get_site("contoso.sharepoint.com", "/sites/Matters")``.
        """
        site_path = path.lstrip("/")
        return self._get_json(f"sites/{quote(hostname)}:/{quote(site_path)}")

    def list_site_drive_items(
        self, site_id: str, *, item_id: str | None = None, top: int | None = None
    ) -> list[Any]:
        """List children of a SharePoint site drive folder.

        Lists the drive root's children, or the children of ``item_id`` when
        given (folder navigation).
        """
        params: dict[str, Any] = {}
        if top is not None:
            params["$top"] = top
        if item_id is not None:
            path = f"sites/{quote(site_id)}/drive/items/{quote(item_id)}/children"
        else:
            path = f"sites/{quote(site_id)}/drive/root/children"
        return list(self._paginate(path, params=params or None))

    def iter_drive_delta(
        self, site_id: str, *, delta_link: str | None = None
    ) -> Iterator[tuple[list[Any], str | None]]:
        """Stream a site drive's ``/delta`` **one page at a time**.

        Yields ``(page_items, resume_cursor)`` per page, where ``resume_cursor`` is
        the ``@odata.nextLink`` for non-final pages (fetch it next to resume the
        enumeration) or the ``@odata.deltaLink`` on the final page (persist it for
        the next **incremental** run). A caller can checkpoint ``resume_cursor``
        after each page, so a mid-enumeration stop resumes from the exact next page
        instead of restarting — essential for large libraries. Pass ``delta_link``
        (a prior nextLink or deltaLink) to resume/incremental; omit for a full
        enumeration. Read-only (GET); one-shot 401 refresh on the first page.
        """
        page: str | None = delta_link or self._url(f"sites/{quote(site_id)}/drive/root/delta")
        headers = self._headers()
        first = True
        while page:
            try:
                data = self._http.get_json(page, headers=headers)
            except HttpError as err:
                if first and "401" in str(err):
                    headers = {"Authorization": f"Bearer {self._ensure_token(force=True)}"}
                    data = self._http.get_json(page, headers=headers)
                else:
                    raise
            first = False
            items = data.get("value", [])
            next_link = data.get("@odata.nextLink")
            # resume cursor: the nextLink while paging, else the final deltaLink
            yield items, (next_link or data.get("@odata.deltaLink"))
            page = next_link

    def list_drive_delta(
        self, site_id: str, *, delta_link: str | None = None
    ) -> tuple[list[Any], str | None]:
        """Enumerate a whole site drive via ``/delta``; return ``(items, delta_link)``.

        Convenience wrapper over :meth:`iter_drive_delta` that buffers all pages —
        fine for small drives/tests. Large libraries should stream via
        :meth:`iter_drive_delta` and checkpoint per page (see the crawler).
        """
        items: list[Any] = []
        cursor: str | None = None
        for page_items, resume_cursor in self.iter_drive_delta(site_id, delta_link=delta_link):
            items.extend(page_items)
            cursor = resume_cursor
        return items, cursor

    def download_drive_item(self, site_id: str, item_id: str) -> bytes:
        """Download a drive item's binary content (read-only GET).

        ``/content`` 302-redirects to a short-lived storage URL; ``requests``
        follows the redirect and drops the ``Authorization`` header on the host
        change, so the pre-authenticated download completes without leaking the
        bearer token.
        """
        url = self._url(f"sites/{quote(site_id)}/drive/items/{quote(item_id)}/content")
        try:
            return self._http.get(url, headers=self._headers()).content
        except HttpError as err:
            if "401" in str(err):
                headers = {"Authorization": f"Bearer {self._ensure_token(force=True)}"}
                return self._http.get(url, headers=headers).content
            raise

    def search_files(self, site_id: str, query: str, *, top: int | None = None) -> list[Any]:
        """Search a SharePoint site drive for files matching ``query``.

        Uses the drive ``search(q=...)`` function, which is a GET (read-only).
        NOTE: blocked app-only on HT's legacy NetDocs libraries — the crawler +
        Knowledge Base path (``list_drive_delta`` + ``download_drive_item``) is
        used for document retrieval instead. Retained for non-legacy sites/tests.
        """
        params: dict[str, Any] = {}
        if top is not None:
            params["$top"] = top
        path = f"sites/{quote(site_id)}/drive/root/search(q='{quote(query)}')"
        return list(self._paginate(path, params=params or None))

    def search(self, query: str, *, entity_types: list[str] | None = None) -> list[Any]:
        """Run a cross-content Graph search via ``POST /search/query``.

        Despite using HTTP POST, ``/search/query`` is a read-only operation: it
        returns matching items and mutates nothing. It is the only supported way
        to run a Microsoft Graph search, so it is used here for read-only
        correspondence/document discovery.
        """
        body = {
            "requests": [
                {
                    "entityTypes": entity_types or ["driveItem", "message"],
                    "query": {"queryString": query},
                }
            ]
        }
        url = self._url("search/query")
        try:
            response = self._http.session.post(
                url, json=body, headers=self._headers(), timeout=self._http.timeout
            )
        except HttpError as err:  # pragma: no cover - defensive
            raise HttpError(f"Graph search failed: {err}") from err
        if response.status_code == 401:
            response = self._http.session.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {self._ensure_token(force=True)}"},
                timeout=self._http.timeout,
            )
        if response.status_code >= 400:
            raise HttpError(f"Graph search failed: {response.status_code}")
        return response.json().get("value", [])
