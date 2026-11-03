"""Read-only QuickBooks Online (QBO) connector.

Provides a matter financial snapshot for reconciliation by reading company info,
invoices, payments, and accounts from QuickBooks Online. All data access is
GET-only; the single POST is the OAuth 2.0 refresh-token exchange required to
mint a short-lived bearer token. No write-back to QuickBooks ever occurs.

Credentials live in AWS Secrets Manager under ``aphrodite/quickbooks`` with
the shape::

    {
        "client_id": "...",
        "client_secret": "...",
        "refresh_token": "...",
        "realm_id": "...",
        "environment": "sandbox" | "production"
    }
"""

from __future__ import annotations

import base64
import time
from typing import Any

from services.http import HttpClient, HttpError
from services.logging import get_logger
from services.secrets import SecretsClient

logger = get_logger(__name__)

# Intuit's OAuth token endpoint is shared across sandbox and production.
TOKEN_URL = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"

API_BASE_URLS = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}

# Refresh slightly before the reported expiry to avoid mid-flight 401s.
TOKEN_EXPIRY_SKEW_SECONDS = 60


def _sql_quote(value: str) -> str:
    """Escape a value for a QBO SQL string literal (double single quotes)."""
    return str(value).replace("'", "''")


class QuickBooksConnector:
    """Read-only connector for QuickBooks Online.

    Auth is lazy: the bearer token is fetched on first use and refreshed when it
    nears expiry. The base URL is chosen from the secret's ``environment`` field.
    """

    def __init__(
        self,
        secrets: SecretsClient,
        http: HttpClient | None = None,
        secret_name: str = "aphrodite/quickbooks",
    ) -> None:
        self._secrets = secrets
        self._http = http or HttpClient()
        self._secret_name = secret_name

        self._access_token: str | None = None
        self._token_expires_at: float = 0.0
        self._base_url: str | None = None
        self._realm_id: str | None = None

    # -- credentials / auth ------------------------------------------------

    def _credentials(self) -> dict[str, Any]:
        return self._secrets.get(self._secret_name)

    @property
    def base_url(self) -> str:
        """API base URL derived from the secret's ``environment`` field."""
        if self._base_url is None:
            creds = self._credentials()
            environment = str(creds.get("environment", "sandbox")).lower()
            try:
                self._base_url = API_BASE_URLS[environment]
            except KeyError:
                raise ValueError(
                    f"Unknown QuickBooks environment {environment!r}; "
                    f"expected one of {sorted(API_BASE_URLS)}"
                ) from None
        return self._base_url

    @property
    def realm_id(self) -> str:
        """QuickBooks company (realm) id from the secret."""
        if self._realm_id is None:
            realm_id = self._credentials().get("realm_id")
            if not realm_id:
                raise ValueError("QuickBooks secret is missing 'realm_id'")
            self._realm_id = str(realm_id)
        return self._realm_id

    def _exchange_refresh_token(self, creds: dict[str, Any]) -> dict[str, Any]:
        """POST the OAuth refresh grant; return Intuit's token payload or raise.

        This is the only non-GET request the connector makes.
        """
        client_id = creds.get("client_id")
        client_secret = creds.get("client_secret")
        refresh_token = creds.get("refresh_token")
        if not (client_id and client_secret and refresh_token):
            raise ValueError(
                "QuickBooks secret must contain 'client_id', 'client_secret', and 'refresh_token'"
            )

        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        response = self._http.session.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": refresh_token},
            headers={
                "Authorization": f"Basic {basic}",
                "Accept": "application/json",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=self._http.timeout,
        )
        if response.status_code >= 400:
            logger.error(
                "QuickBooks token refresh failed",
                extra={"extra_fields": {"status": response.status_code}},
            )
            raise HttpError(f"QuickBooks token refresh failed: {response.status_code}")
        return response.json()

    def _refresh_access_token(self) -> str:
        """Exchange the stored refresh token for a fresh bearer access token.

        Intuit rotates the refresh token and invalidates the previous one, which
        makes concurrent Lambda invocations race: another invocation can rotate
        first, leaving this container's *cached* secret holding a dead token —
        and since the cache never re-reads, every refresh in the warm container
        then fails ("token refresh failed: 400" during the round-2 re-tests).
        Hardening, no cross-invocation lock (POC scale — a residual same-second
        race self-heals the same way on the loser's next call):

        - on a refresh failure, re-read the secret from Secrets Manager and retry
          ONCE iff it holds a different (newer) refresh token;
        - before persisting a rotation, re-read and skip the write if a third,
          newer token is already stored (last-writer-wins would re-break it).
        """
        creds = self._credentials()
        tried = str(creds.get("refresh_token") or "")
        try:
            payload = self._exchange_refresh_token(creds)
        except HttpError:
            fresh = self._secrets.get(self._secret_name, refresh=True)
            if str(fresh.get("refresh_token") or "") in ("", tried):
                raise  # same token stored → genuinely dead; needs re-authorization
            logger.info("QuickBooks refresh token was rotated concurrently; retrying with stored")
            creds = fresh
            payload = self._exchange_refresh_token(creds)

        access_token = payload.get("access_token")
        if not access_token:
            raise HttpError("QuickBooks token refresh returned no access_token")

        expires_in = float(payload.get("expires_in", 3600))
        self._access_token = access_token
        self._token_expires_at = time.monotonic() + expires_in - TOKEN_EXPIRY_SKEW_SECONDS

        # Persist a rotated refresh token so the next refresh (incl. after a cold
        # start) stays valid. Intuit invalidates the prior token after rotation.
        exchanged = str(creds.get("refresh_token") or "")
        rotated = payload.get("refresh_token")
        if rotated and rotated != exchanged:
            stored = self._secrets.get(self._secret_name, refresh=True)
            stored_token = str(stored.get("refresh_token") or "")
            if stored_token not in ("", exchanged, rotated):
                # A concurrent invocation stored a token from a different exchange
                # chain — clobbering it with ours could persist a dead token.
                logger.info("QuickBooks refresh token already superseded; not persisting ours")
            else:
                stored["refresh_token"] = rotated
                self._secrets.put(self._secret_name, stored)

        return access_token

    def _bearer_token(self) -> str:
        if self._access_token is None or time.monotonic() >= self._token_expires_at:
            return self._refresh_access_token()
        return self._access_token

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._bearer_token()}",
            "Accept": "application/json",
        }

    def _company_url(self, path: str) -> str:
        return f"{self.base_url}/v3/company/{self.realm_id}/{path}"

    def _get(self, path: str, *, params: dict[str, Any] | None = None) -> Any:
        return self._http.get_json(
            self._company_url(path),
            headers=self._headers(),
            params=params,
        )

    # -- read methods ------------------------------------------------------

    def get_company_info(self) -> dict[str, Any]:
        """Return the QuickBooks company profile (CompanyInfo entity)."""
        data = self._get(f"companyinfo/{self.realm_id}")
        return data.get("CompanyInfo", data)

    def query(self, statement: str) -> list[dict[str, Any]]:
        """Run a read-only QBO SQL-like query and return the matched entities.

        QuickBooks returns results under ``QueryResponse`` keyed by entity name
        (e.g. ``Invoice``). The list for the queried entity is returned; an empty
        list is returned when nothing matches.
        """
        data = self._get("query", params={"query": statement})
        response = data.get("QueryResponse", {})
        for value in response.values():
            if isinstance(value, list):
                return value
        return []

    def find_customers(self, name: str, *, max_results: int = 10) -> list[dict[str, Any]]:
        """Best-effort customer lookup by display name (fallback matcher).

        Used only when a matter has no Clio ref; the preferred, stable matcher is
        :meth:`find_customer_by_clio_id`.
        """
        return self.query(
            f"SELECT * FROM Customer WHERE DisplayName LIKE '%{_sql_quote(name)}%' "
            f"MAXRESULTS {int(max_results)}"
        )

    def find_customer_by_clio_id(
        self, clio_id: str, *, max_results: int = 5
    ) -> list[dict[str, Any]]:
        """Look up the QuickBooks customer by the Clio id in its DisplayName, e.g.
        ``"Verner Elwood Kober (Clio 2369563557)"``.

        Clio's QuickBooks Online sync creates a QBO customer per Clio CONTACT (the
        client) and stamps that **client-contact id** into the DisplayName — so
        ``clio_id`` here is the matter's client-contact id, NOT the matter id.
        Matching on the id (not the name) is stable across Clio renames: a rename
        creates a NEW customer, so name matching would split records across
        duplicates and miss/double-count them (per HT's finance team). The caller
        verifies the exact ``(Clio <id>)`` token, since ``LIKE`` is a substring match.
        """
        token = f"(Clio {_sql_quote(str(clio_id))})"
        return self.query(
            f"SELECT * FROM Customer WHERE DisplayName LIKE '%{token}%' "
            f"MAXRESULTS {int(max_results)}"
        )

    def list_invoices(
        self, *, customer_id: str | None = None, max_results: int = 100
    ) -> list[dict[str, Any]]:
        """Return invoices, optionally scoped to one customer (reconciliation)."""
        where = f"WHERE CustomerRef = '{_sql_quote(customer_id)}' " if customer_id else ""
        return self.query(f"SELECT * FROM Invoice {where}MAXRESULTS {int(max_results)}")

    def list_payments(
        self, *, customer_id: str | None = None, max_results: int = 100
    ) -> list[dict[str, Any]]:
        """Return payments, optionally scoped to one customer (reconciliation)."""
        where = f"WHERE CustomerRef = '{_sql_quote(customer_id)}' " if customer_id else ""
        return self.query(f"SELECT * FROM Payment {where}MAXRESULTS {int(max_results)}")

    def list_accounts(self, *, max_results: int = 100) -> list[dict[str, Any]]:
        """Return chart-of-accounts entries."""
        return self.query(f"SELECT * FROM Account MAXRESULTS {int(max_results)}")
