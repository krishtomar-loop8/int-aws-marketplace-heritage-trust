"""Read-only Clio Manage API v4 connector.

Reads matters, contacts, notes, activities, calendar entries, tasks, and the
trust/client financial ledgers (bank accounts, bank transactions, bills) from
the customer's Clio account. All data access is GET-only; the single POST is
the OAuth 2.0 refresh-token exchange required to mint a short-lived bearer
token. No write-back to Clio ever occurs.

Clio is regional and region-locked: an account lives in exactly one pod (US
``app.clio.com``, CA ``ca.app.clio.com``, EU, AU) and its OAuth credentials only
work against that pod. The base URL defaults to the US pod (``app.clio.com``)
since that's the most common pod, but is overridable via the ``base_url``
field of the secret (or the constructor) so the same code serves any region's
account unchanged. The OAuth token endpoint is derived from the base URL's
origin, keeping host and region in lock-step.

Clio paces at ~50 requests/minute (the tightest source in the suite), so the
connector builds a client-side token-bucket limiter by default and uses the
``fields`` query parameter to pull nested associations in a single call rather
than fanning out N+1 follow-up requests. List endpoints page at 200 records via
``meta.paging.next``.

Credentials live in AWS Secrets Manager under ``aphrodite/clio`` with the
shape::

    {
        "client_id": "...",
        "client_secret": "...",
        "refresh_token": "...",
        "base_url": "https://app.clio.com/api/v4"   # optional; defaults to US
    }
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

from services.http import HttpClient, HttpError, RateLimiter
from services.logging import get_logger
from services.secrets import SecretsClient

logger = get_logger(__name__)

# Defaults to the US pod; other-region accounts override via the secret's
# ``base_url``. Tokens are region-locked to the base URL's host.
DEFAULT_BASE_URL = "https://app.clio.com/api/v4"

# Clio publishes a 50 request/minute ceiling; pace beneath it and allow a small
# burst up to the full bucket.
RATE_PER_SECOND = 50 / 60.0
RATE_CAPACITY = 50

# Default page size; Clio caps list endpoints at 200 records per page.
DEFAULT_PAGE_SIZE = 200

# Refresh slightly before the reported expiry to avoid mid-flight 401s.
TOKEN_EXPIRY_SKEW_SECONDS = 60


class ClioConnector:
    """Read-only connector for the Clio Manage API v4.

    Defaults to the US pod (``app.clio.com``); pass ``base_url`` or set it in
    the secret to target another region.

    Auth is lazy: the bearer token is fetched on first use and refreshed when it
    nears expiry or after a 401. When no :class:`HttpClient` is supplied a
    default one is created with a token-bucket rate limiter sized to Clio's
    50 req/min ceiling.
    """

    def __init__(
        self,
        secrets: SecretsClient,
        http: HttpClient | None = None,
        secret_name: str = "aphrodite/clio",
        base_url: str | None = None,
    ) -> None:
        self._secrets = secrets
        self._http = http or HttpClient(
            rate_limiter=RateLimiter(rate_per_sec=RATE_PER_SECOND, capacity=RATE_CAPACITY)
        )
        self._secret_name = secret_name
        self._base_url_override = base_url.rstrip("/") if base_url else None
        self._base_url_resolved: str | None = None

        self._access_token: str | None = None
        self._token_expires_at: float = 0.0

    # -- credentials / auth ------------------------------------------------

    def _credentials(self) -> dict[str, Any]:
        return self._secrets.get(self._secret_name)

    @property
    def base_url(self) -> str:
        """API base URL: explicit override, then the secret, then the US default.

        Clio is region-locked, so this also pins the OAuth token host
        (:attr:`_token_url`).
        """
        if self._base_url_resolved is None:
            if self._base_url_override is not None:
                self._base_url_resolved = self._base_url_override
            else:
                configured = self._credentials().get("base_url")
                self._base_url_resolved = (
                    str(configured).rstrip("/") if configured else DEFAULT_BASE_URL
                )
        return self._base_url_resolved

    @property
    def _token_url(self) -> str:
        """OAuth token endpoint, derived from the base URL's origin (same pod)."""
        parts = urlsplit(self.base_url)
        return f"{parts.scheme}://{parts.netloc}/oauth/token"

    def _refresh_access_token(self) -> str:
        """Exchange the stored refresh token for a fresh bearer access token.

        This is the only non-GET request the connector makes. Clio rotates the
        refresh token on refresh and invalidates the prior one, so concurrent
        Lambda invocations (and cold starts) race: another can rotate first,
        leaving this container's cached secret holding a dead token. Mirroring the
        QuickBooks hardening (POC scale, no cross-invocation lock — a residual
        same-second race self-heals on the loser's next call):

        - on a refresh failure, re-read the secret and retry ONCE iff a newer
          token is stored;
        - persist the rotated token back to Secrets Manager (best-effort) so it
          survives a cold start rather than living only in this process's cache.
        """
        creds = self._credentials()
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")
        tried = str(creds.get("refresh_token") or "")
        if not (client_id and client_secret and tried):
            raise ValueError(
                "Clio secret must contain 'client_id', 'client_secret', and 'refresh_token'"
            )

        try:
            payload = self._exchange_refresh_token(creds)
        except HttpError:
            fresh = self._secrets.get(self._secret_name, refresh=True)
            if str(fresh.get("refresh_token") or "") in ("", tried):
                raise  # same token stored → genuinely dead; needs re-authorization
            logger.info("Clio refresh token was rotated concurrently; retrying with stored")
            creds = fresh
            payload = self._exchange_refresh_token(creds)

        access_token = payload.get("access_token")
        if not access_token:
            raise HttpError("Clio token refresh returned no access_token")

        expires_in = float(payload.get("expires_in", 2_592_000))
        self._access_token = access_token
        self._token_expires_at = time.monotonic() + expires_in - TOKEN_EXPIRY_SKEW_SECONDS

        exchanged = str(creds.get("refresh_token") or "")
        rotated = payload.get("refresh_token")
        if rotated and rotated != exchanged:
            self._persist_rotated_token(exchanged, rotated)

        return access_token

    def _exchange_refresh_token(self, creds: dict[str, Any]) -> dict[str, Any]:
        """POST the refresh-token grant; return the parsed payload (raises on >=400)."""
        response = self._http.session.post(
            self._token_url,
            data={
                "grant_type": "refresh_token",
                "client_id": creds.get("client_id"),
                "client_secret": creds.get("client_secret"),
                "refresh_token": creds.get("refresh_token"),
            },
            headers={
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=self._http.timeout,
        )
        if response.status_code >= 400:
            logger.error(
                "Clio token refresh failed",
                extra={"extra_fields": {"status": response.status_code}},
            )
            raise HttpError(f"Clio token refresh failed: {response.status_code}")
        return response.json()

    def _persist_rotated_token(self, exchanged: str, rotated: str) -> None:
        """Write a rotated refresh token back to Secrets Manager (best-effort).

        Re-read first and skip the write if a third, newer token is already stored
        (last-writer-wins would re-break it). Never raises — the access token is
        already valid, and a failed write degrades to the in-memory value plus the
        401-refresh-retry in :meth:`_get_json` / :meth:`_paginate`.
        """
        try:
            stored = self._secrets.get(self._secret_name, refresh=True)
            stored_token = str(stored.get("refresh_token") or "")
            if stored_token not in ("", exchanged, rotated):
                logger.info("Clio refresh token already superseded; not persisting ours")
                return
            stored["refresh_token"] = rotated
            self._secrets.put(self._secret_name, stored)
        except Exception:  # noqa: BLE001 - persistence is best-effort; token already valid
            logger.warning("failed to persist rotated Clio refresh token")

    def _bearer_token(self, *, force_refresh: bool = False) -> str:
        expired = self._access_token is None or time.monotonic() >= self._token_expires_at
        if force_refresh or expired:
            return self._refresh_access_token()
        return self._access_token

    def _headers(self, *, force_refresh: bool = False) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._bearer_token(force_refresh=force_refresh)}",
            "Accept": "application/json",
        }

    # -- request helpers ---------------------------------------------------

    def _url(self, resource: str) -> str:
        return f"{self.base_url}/{resource}"

    def _get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        """GET a single payload, refreshing the token once on a 401."""
        try:
            return self._http.get_json(url, headers=self._headers(), params=params)
        except HttpError as exc:
            if " 401" not in str(exc):
                raise
            logger.info("Clio returned 401; refreshing access token and retrying")
            return self._http.get_json(
                url, headers=self._headers(force_refresh=True), params=params
            )

    def _paginate(
        self, resource: str, *, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield records across pages following ``meta.paging.next``.

        Recovers from a stale cached access token the same way :meth:`_get_json`
        does: if the **first** page returns 401, refresh the token and retry once.
        The retry is guarded to before any record is yielded — a mid-walk 401
        propagates instead, since restarting pagination there would double-yield.
        """
        request_params = {"limit": DEFAULT_PAGE_SIZE, **(params or {})}
        url = self._url(resource)
        page_kwargs: dict[str, Any] = dict(
            items=lambda d: d.get("data", []),
            next_url=lambda d: d.get("meta", {}).get("paging", {}).get("next"),
            params=request_params,
        )
        self._bearer_token()  # ensure a token before paging
        emitted = False
        try:
            for record in self._http.paginate(url, headers=self._headers(), **page_kwargs):
                emitted = True
                yield record
        except HttpError as exc:
            if emitted or " 401" not in str(exc):
                raise
            logger.info("Clio returned 401 at pagination start; refreshing token and retrying")
            yield from self._http.paginate(
                url, headers=self._headers(force_refresh=True), **page_kwargs
            )

    @staticmethod
    def _with_fields(params: dict[str, Any], fields: str | None) -> dict[str, Any]:
        if fields:
            params["fields"] = fields
        return params

    # -- matters & contacts ------------------------------------------------

    def search_matters(
        self, query: str, *, fields: str | None = None, **params: Any
    ) -> list[dict[str, Any]]:
        """Search matters by free-text ``query`` (client, description, number)."""
        search = self._with_fields({"query": query, **params}, fields)
        return list(self._paginate("matters.json", params=search))

    def list_matters(
        self,
        *,
        status: str | None = "open",
        fields: str | None = None,
        limit: int = 500,
        **params: Any,
    ) -> list[dict[str, Any]]:
        """List matters (no query), optionally filtered by ``status`` (open/pending/closed).

        Capped at ``limit`` records to bound the payload/latency; a truncation is
        logged so a silently short list can't masquerade as complete.
        """
        if status:
            params["status"] = status
        items: list[dict[str, Any]] = []
        for matter in self._paginate("matters.json", params=self._with_fields(params, fields)):
            items.append(matter)
            if len(items) >= limit:
                logger.info(
                    "list_matters truncated at limit",
                    extra={"extra_fields": {"limit": limit, "status": status}},
                )
                break
        return items

    def get_matter(self, matter_id: int | str, *, fields: str | None = None) -> dict[str, Any]:
        """Return a single matter, optionally hydrating nested associations."""
        params = self._with_fields({}, fields)
        data = self._get_json(self._url(f"matters/{matter_id}.json"), params=params or None)
        return data.get("data", data)

    def list_contacts(
        self, *, query: str | None = None, fields: str | None = None, **params: Any
    ) -> list[dict[str, Any]]:
        """Return contacts, optionally filtered by free-text ``query``."""
        if query is not None:
            params["query"] = query
        return list(self._paginate("contacts.json", params=self._with_fields(params, fields)))

    def get_contact(self, contact_id: int | str, *, fields: str | None = None) -> dict[str, Any]:
        """Return a single contact."""
        params = self._with_fields({}, fields)
        data = self._get_json(self._url(f"contacts/{contact_id}.json"), params=params or None)
        return data.get("data", data)

    # -- matter detail -----------------------------------------------------

    def list_matter_notes(
        self, matter_id: int | str, *, fields: str | None = None, **params: Any
    ) -> list[dict[str, Any]]:
        """Return notes attached to a matter (Clio requires ``type=Matter``)."""
        params.update({"matter_id": matter_id, "type": "Matter"})
        return list(self._paginate("notes.json", params=self._with_fields(params, fields)))

    def list_activities(
        self, matter_id: int | str | None = None, *, fields: str | None = None, **params: Any
    ) -> list[dict[str, Any]]:
        """Return time/expense activity entries, optionally scoped to a matter."""
        if matter_id is not None:
            params["matter_id"] = matter_id
        return list(self._paginate("activities.json", params=self._with_fields(params, fields)))

    def list_calendar_entries(
        self, matter_id: int | str | None = None, *, fields: str | None = None, **params: Any
    ) -> list[dict[str, Any]]:
        """Return calendar entries (deadlines/events), optionally for a matter."""
        if matter_id is not None:
            params["matter_id"] = matter_id
        return list(
            self._paginate("calendar_entries.json", params=self._with_fields(params, fields))
        )

    def list_tasks(
        self, matter_id: int | str | None = None, *, fields: str | None = None, **params: Any
    ) -> list[dict[str, Any]]:
        """Return tasks, optionally scoped to a matter."""
        if matter_id is not None:
            params["matter_id"] = matter_id
        return list(self._paginate("tasks.json", params=self._with_fields(params, fields)))

    def list_relationships(
        self, matter_id: int | str | None = None, *, fields: str | None = None, **params: Any
    ) -> list[dict[str, Any]]:
        """Return matter↔contact relationships, optionally scoped to a matter.

        Each relationship links a contact to the matter with a role ``description``
        (e.g. "Trustee", "Beneficiary"). Clio v4 exposes these on this separate
        endpoint — they cannot be hydrated as a nested field on the matter.
        """
        if matter_id is not None:
            params["matter_id"] = matter_id
        return list(self._paginate("relationships.json", params=self._with_fields(params, fields)))

    # -- financial ledgers -------------------------------------------------

    def list_bank_accounts(
        self, *, fields: str | None = None, **params: Any
    ) -> list[dict[str, Any]]:
        """Return bank/trust accounts (FIRM-WIDE balances, not matter-scoped)."""
        return list(self._paginate("bank_accounts.json", params=self._with_fields(params, fields)))

    def get_matter_account_balances(self, matter_id: int | str) -> list[dict[str, Any]]:
        """Return the matter's per-account balances — its trust/operating ledger.

        Clio exposes this as the Matter's ``account_balances`` field: one row per
        firm account the matter holds funds in, with the MATTER-scoped balance
        (unlike ``list_bank_accounts``, whose balances are firm-wide). Verified
        live: valid sub-fields are ``id, name, type ("Trust"/"Operating"),
        balance, currency_id``. Returns ``[]`` when the matter has no ledger rows.
        """
        data = self.get_matter(
            matter_id, fields="id,account_balances{id,name,type,balance}"
        )
        balances = data.get("account_balances") if isinstance(data, dict) else None
        return balances if isinstance(balances, list) else []

    def list_bank_transactions(
        self,
        bank_account_id: int | str | None = None,
        *,
        matter_id: int | str | None = None,
        fields: str | None = None,
        **params: Any,
    ) -> list[dict[str, Any]]:
        """Return trust-ledger transactions, scopable by bank account or matter.

        ``matter_id`` filtering is supported by Clio v4 (verified live) and
        returns only that matter's ledger entries.
        """
        if bank_account_id is not None:
            params["bank_account_id"] = bank_account_id
        if matter_id is not None:
            params["matter_id"] = matter_id
        return list(
            self._paginate("bank_transactions.json", params=self._with_fields(params, fields))
        )

    def list_bills(
        self, matter_id: int | str | None = None, *, fields: str | None = None, **params: Any
    ) -> list[dict[str, Any]]:
        """Return bills (client ledger), optionally scoped to a matter."""
        if matter_id is not None:
            params["matter_id"] = matter_id
        return list(self._paginate("bills.json", params=self._with_fields(params, fields)))
