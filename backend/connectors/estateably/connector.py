"""Read-only Estateably connector.

Estateably is the customer's estate/trust administration system of record. This
connector reads matter (estate) status, phases, tasks, contacts, beneficiaries,
executors, client-intake state, documents (including binary content), notes,
activities and financials so the Global Matter Search agent and the quarterly
report drafts can synthesise a source-cited view of a file.

All data access is GET-only. The single non-GET request is the JSON:API login
``POST`` required to mint a short-lived JWT bearer token. No write-back to
Estateably ever occurs.

The API is a JSON:API service: requests and responses use the
``application/vnd.api+json`` media type, resources are returned under ``data``
(with ``id``/``type``/``attributes``), and collections are paginated with
``page[number]`` / ``page[size]`` query parameters (totals reported under
``meta.page.*`` and ``meta.items.count``).

Credentials live in AWS Secrets Manager under ``aphrodite/estateably`` with
the shape::

    {
        "username": "...",
        "password": "...",
        "base_url": "https://app.estateably.com",  # optional
        "org_id": "..."                              # optional, for global search
    }
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from services.http import HttpClient, HttpError
from services.logging import get_logger
from services.secrets import SecretsClient

logger = get_logger(__name__)

DEFAULT_BASE_URL = "https://app.estateably.com"

# JSON:API media type used by Estateably for both Content-Type and Accept.
JSON_API_MEDIA_TYPE = "application/vnd.api+json"

# Default page size for paginated collection reads.
DEFAULT_PAGE_SIZE = 100

# Hard backstop on pages walked, in case an endpoint ignores pagination and never
# reports a page count (some collections, e.g. contacts, do this and would loop
# forever otherwise). Paired with a no-progress check below.
MAX_PAGES = 100

# Estateably's edge/WAF rejects the default python-requests User-Agent with a 403,
# so identify the integration explicitly on every request.
USER_AGENT = "Loop8-Aphrodite-Integration/1.0"


class EstateablyConnector:
    """Read-only connector for Estateably (JSON:API).

    Auth is lazy: the JWT is fetched on first use. Estateably expires the token
    after roughly 25 minutes of inactivity, so any read that returns ``401`` is
    retried exactly once after re-authenticating.
    """

    def __init__(
        self,
        secrets: SecretsClient,
        http: HttpClient | None = None,
        secret_name: str = "aphrodite/estateably",
        base_url: str | None = None,
    ) -> None:
        self._secrets = secrets
        self._http = http or HttpClient()
        self._secret_name = secret_name
        self._base_url_override = base_url.rstrip("/") if base_url else None

        self._token: str | None = None
        self._base_url: str | None = None

    # -- credentials / auth ------------------------------------------------

    def _credentials(self, *, refresh: bool = False) -> dict[str, Any]:
        return self._secrets.get(self._secret_name, refresh=refresh)

    @property
    def base_url(self) -> str:
        """API base URL: explicit override, then secret, then the public default."""
        if self._base_url is None:
            if self._base_url_override is not None:
                self._base_url = self._base_url_override
            else:
                configured = self._credentials().get("base_url")
                self._base_url = str(configured).rstrip("/") if configured else DEFAULT_BASE_URL
        return self._base_url

    def _login(self, *, refresh_secret: bool = False) -> str:
        """Authenticate with username/password and return a fresh JWT.

        This is the only non-GET request the connector makes. The token is read
        from ``data.attributes.token`` per the Estateably login schema.
        ``refresh_secret`` forces a fresh Secrets Manager read instead of the
        cached value — used on a 401 retry so a reconnected (changed) secret
        takes effect immediately rather than only after a cold start.
        """
        creds = self._credentials(refresh=refresh_secret)
        username = creds.get("username")
        password = creds.get("password")
        if not (username and password):
            raise ValueError("Estateably secret must contain 'username' and 'password'")

        response = self._http.session.post(
            f"{self.base_url}/api/v1/auth/login",
            json={
                "data": {
                    "type": "auth",
                    "attributes": {"username": username, "password": password},
                }
            },
            headers={
                "Content-Type": JSON_API_MEDIA_TYPE,
                "Accept": JSON_API_MEDIA_TYPE,
                "User-Agent": USER_AGENT,
            },
            timeout=self._http.timeout,
        )
        if response.status_code >= 400:
            logger.error(
                "Estateably login failed",
                extra={"extra_fields": {"status": response.status_code}},
            )
            raise HttpError(f"Estateably login failed: {response.status_code}")

        attributes = response.json().get("data", {}).get("attributes", {})
        token = attributes.get("token")
        if not token:
            raise HttpError("Estateably login returned no token")

        self._token = str(token)
        return self._token

    def _bearer_token(self) -> str:
        if self._token is None:
            return self._login()
        return self._token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._bearer_token()}",
            "Accept": JSON_API_MEDIA_TYPE,
            "User-Agent": USER_AGENT,
        }

    def _url(self, path: str) -> str:
        return f"{self.base_url}/api/v1/{path.lstrip('/')}"

    # -- request helpers ---------------------------------------------------

    def _get_response(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> Any:
        """GET ``url``, re-authenticating once on a 401 (expired JWT).

        ``extra_headers`` lets specific endpoints add request headers (e.g. the
        ``orgId`` header required by global search) on top of the auth headers.
        """
        try:
            headers = self._headers()
            if extra_headers:
                headers.update(extra_headers)
            return self._http.get(url, headers=headers, params=params)
        except HttpError as exc:
            # Re-auth only on a genuine 401 (expired JWT). Match the structured
            # status, not the message text — a substring check would misfire on a
            # body that merely contains "401" and, worse, would not distinguish a
            # 401 from a gateway 402/404.
            if getattr(exc, "status", None) != 401:
                raise
            logger.info("Estateably token expired; re-authenticating")
            self._token = None
            self._login(refresh_secret=True)
            headers = self._headers()
            if extra_headers:
                headers.update(extra_headers)
            return self._http.get(url, headers=headers, params=params)

    def _get_json(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self._get_response(self._url(path), params=params).json()

    def _get_one(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the single ``data`` resource from a JSON:API response."""
        data = self._get_json(path, params=params).get("data")
        return data if isinstance(data, dict) else {}

    def _get_list(self, path: str, *, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Return the ``data`` list from a single (un-paginated) GET.

        Estate sub-resources (contacts, beneficiaries, executors, documents,
        tasks, accounts, inventory, comments) return the whole collection in one
        response (``meta.page.count`` is 1). They also mishandle ``page[...]``
        params — some ignore them (looping a paginator), and ``beneficiaries``
        rejects them with a 400 — so these are fetched in a single GET, not via
        :meth:`_paginate`.
        """
        data = self._get_json(path, params=params).get("data")
        return data if isinstance(data, list) else []

    @staticmethod
    def _page_params(number: int, size: int) -> dict[str, Any]:
        """JSON:API page-based pagination params, flattened to ``page[...]`` keys."""
        return {"page[number]": number, "page[size]": size}

    def _paginate(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Iterator[dict[str, Any]]:
        """Yield every ``data`` item across a paginated JSON:API collection.

        Pages are walked by incrementing ``page[number]`` until the reported page
        total (``meta.page.count``) is reached or an empty page is returned. Some
        collections (e.g. ``contacts``) ignore the page params and return the
        same page forever with no page count, so we also stop when a page yields
        **no new ids** (no forward progress) and cap the total pages walked.
        """
        number = 1
        base_params = dict(params or {})
        seen: set[str] = set()
        while number <= MAX_PAGES:
            page_params = {**base_params, **self._page_params(number, page_size)}
            payload = self._get_json(path, params=page_params)
            items = payload.get("data")
            if not isinstance(items, list) or not items:
                return

            fresh = [it for it in items if str(it.get("id")) not in seen]
            if not fresh:  # endpoint isn't advancing → stop rather than loop
                return
            for it in fresh:
                seen.add(str(it.get("id")))
            yield from fresh

            page_meta = payload.get("meta", {}).get("page", {})
            total_pages = page_meta.get("count")
            if total_pages is not None and number >= int(total_pages):
                return
            number += 1

    # -- read methods ------------------------------------------------------

    def global_search(
        self, query: str, *, page_size: int = DEFAULT_PAGE_SIZE
    ) -> list[dict[str, Any]]:
        """Search across estates/contacts for a matter or client name.

        Backed by ``GET /api/v1/globalSearch``. The org id (required by the
        endpoint as a header) is read from the secret when present.
        """
        org_id = self._credentials().get("org_id")
        extra_headers = {"orgId": str(org_id)} if org_id else None

        results: list[dict[str, Any]] = []
        seen: set[str] = set()
        number = 1
        while number <= MAX_PAGES:
            params = {"search": query, **self._page_params(number, page_size)}
            payload = self._get_response(
                self._url("globalSearch"), params=params, extra_headers=extra_headers
            ).json()
            items = payload.get("data")
            if not isinstance(items, list) or not items:
                break
            fresh = [it for it in items if str(it.get("id")) not in seen]
            if not fresh:  # no forward progress → stop rather than loop
                break
            for it in fresh:
                seen.add(str(it.get("id")))
            results.extend(fresh)
            page_meta = payload.get("meta", {}).get("page", {})
            total_pages = page_meta.get("count")
            if total_pages is not None and number >= int(total_pages):
                break
            number += 1
        return results

    def get_estate(self, estate_id: str) -> dict[str, Any]:
        """Return a single estate (matter): type, title, status/stage, dates."""
        return self._get_one(f"estates/{estate_id}")

    def get_estate_summary(self, estate_id: str) -> dict[str, Any]:
        """Return the estate summary view (high-level status snapshot)."""
        return self._get_one(f"estates/{estate_id}/summary")

    def list_estates(self, *, page_size: int = DEFAULT_PAGE_SIZE) -> list[dict[str, Any]]:
        """Return all estate cases (used by the bulk AML active-client roster)."""
        return list(self._paginate("estates", page_size=page_size))

    def get_workflow(self, estate_id: str) -> dict[str, Any]:
        """Return the estate's workflow (the live source of stage/phase state).

        Shape: ``attributes`` with ``nodes`` (the phases/steps), ``context``
        (current state), and ``template``. The agent derives "current stage"
        from this. (The spec's ``/estates/{id}/phases`` collection is not served
        by the production instance — it returns 404 — so workflow is used.)
        """
        return self._get_one(f"estates/{estate_id}/workflow")

    def list_phases(self, estate_id: str) -> list[dict[str, Any]]:
        """Return the workflow nodes (phases/steps) for an estate."""
        nodes = self.get_workflow(estate_id).get("attributes", {}).get("nodes")
        return nodes if isinstance(nodes, list) else []

    def list_tasks(self, estate_id: str) -> list[dict[str, Any]]:
        """Return tasks for an estate (deadlines / checklist items).

        Backed by ``GET /api/v1/tasks`` filtered by estate via the JSON:API
        ``filter[estateId]`` query parameter (returned in one response).
        """
        return self._get_list("tasks", params={"filter[estateId]": estate_id})

    def list_contacts(self, estate_id: str) -> list[dict[str, Any]]:
        """Return contacts attached to an estate with their roles."""
        return self._get_list(f"estates/{estate_id}/contacts")

    def list_beneficiaries(self, estate_id: str) -> list[dict[str, Any]]:
        """Return beneficiaries on an estate."""
        return self._get_list(f"estates/{estate_id}/beneficiaries")

    def list_executors(self, estate_id: str) -> list[dict[str, Any]]:
        """Return executors / trustees on an estate."""
        return self._get_list(f"estates/{estate_id}/executors")

    def list_substitute_decision_makers(self, estate_id: str) -> list[dict[str, Any]]:
        """Return substitute decision makers (attorneys / committee) on a case.

        Backs the AML export's Attorney/Committee party rows for POA and
        committeeship matters; records carry name and ``dateOfBirth``.
        """
        return self._get_list(f"estates/{estate_id}/substituteDecisionMakers")

    def get_client_intake(self, estate_id: str) -> dict[str, Any]:
        """Return the client-intake (KYC onboarding) state for an estate.

        Uses ``estates/{id}/intake``; the ``clientIntake`` path returns 403
        ("Access to Estate not authorized") on the production instance.
        """
        return self._get_one(f"estates/{estate_id}/intake")

    def list_documents(self, estate_id: str) -> list[dict[str, Any]]:
        """Return the documents/folders attached to an estate."""
        return self._get_list(f"estates/{estate_id}/documents")

    def download_document(self, document_id: str) -> bytes:
        """Download a document's raw binary content.

        Backed by ``GET /api/v1/documents/{documentId}/file`` — the raw-file
        endpoint (``application/octet-stream``). The ``filePreview`` path is a
        *preview generator* that returns a JSON envelope and 4xx's for files it
        can't render (large scans, unusual MIME types) and for folder/fileless
        rows, so it is not used here. Callers should gate on :meth:`is_downloadable`
        and treat a 403/404 on a single document as a skip (RBAC/absent file).
        """
        response = self._get_response(self._url(f"documents/{document_id}/file"))
        return response.content

    @staticmethod
    def is_downloadable(document: dict[str, Any]) -> bool:
        """True if a ``list_documents`` row has a real, downloadable binary.

        ``list_documents`` mixes folders and structured/workflow rows in with
        actual files; only rows whose (inner) ``type`` is not ``folder`` and that
        carry an ``attributes.file`` object have bytes to fetch. Gating on this
        avoids the guaranteed 4xx from hitting the file endpoint on a folder or a
        fileless row. Accepts a JSON:API item (``{attributes: {...}}``) or a bare
        attributes dict.
        """
        attrs = document.get("attributes")
        attrs = attrs if isinstance(attrs, dict) else document
        if attrs.get("type") == "folder":
            return False
        return bool(attrs.get("file"))

    def list_comments(self, estate_id: str) -> list[dict[str, Any]]:
        """Return comments (file notes) left on an estate."""
        return self._get_list(f"estates/{estate_id}/comments")

    def list_activities(self, case_id: str) -> list[dict[str, Any]]:
        """Return the activity feed for a case/estate."""
        return self._get_list(f"estates/{case_id}/activities")

    def list_accounts(self, estate_id: str) -> list[dict[str, Any]]:
        """Return financial accounts (bank/estate accounts) on an estate."""
        return self._get_list(f"estates/{estate_id}/accounts")

    def list_inventory(self, estate_id: str) -> list[dict[str, Any]]:
        """Return inventory items (assets and liabilities) on an estate."""
        return self._get_list(f"estates/{estate_id}/inventory")
