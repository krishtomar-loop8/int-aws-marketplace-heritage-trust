"""Build source-record deep-links for citations.

A matter-search dossier is assembled for one resolved matter, so every Clio/
Estateably datum belongs to that one matter/estate — those citations link to the
matter in Clio / the estate in Estateably (avoiding fragile per-record-type
links). Outlook/SharePoint items carry their own real URL and don't need these.

Pure functions (no I/O), so they're unit-tested directly. Hosts are derived from
the connector base URLs where possible, so region/tenant is correct.
"""

from __future__ import annotations

from urllib.parse import urlsplit

_CLIO_DEFAULT = "https://app.clio.com"
_ESTATEABLY_DEFAULT = "https://app.estateably.com"

# Estateably web-app deep-links are hash routes and differ by record type:
#   estates: {origin}/#/estates/{id}/overview
#   trusts:  {origin}/#/trusts/{id}/overview
# (confirmed against the live HT tenant). QuickBooks' is still best-effort — VERIFY.
_QBO_HOST = "https://app.qbo.intuit.com"
_QBO_SANDBOX_HOST = "https://app.sandbox.qbo.intuit.com"


def _origin(base_url: str | None, default: str) -> str:
    """Scheme://host of ``base_url`` (strips any /api path); fall back to ``default``."""
    if base_url:
        p = urlsplit(base_url)
        if p.scheme and p.netloc:
            return f"{p.scheme}://{p.netloc}"
    return default


def clio_matter_url(base_url: str | None, matter_id: str) -> str:
    """Clio Manage deep-link to a matter (region from the API base URL's origin)."""
    return f"{_origin(base_url, _CLIO_DEFAULT)}/nc/#/matters/{matter_id}"


def estateably_estate_url(base_url: str | None, estate_id: str, *, is_trust: bool = False) -> str:
    """Estateably web-app deep-link to an estate/trust overview (hash route).

    Trusts live under ``/#/trusts/{id}`` and estates under ``/#/estates/{id}``;
    the origin (tenant) comes from the API base URL.
    """
    segment = "trusts" if is_trust else "estates"
    return f"{_origin(base_url, _ESTATEABLY_DEFAULT)}/#/{segment}/{estate_id}/overview"


def estateably_is_trust(matter_type: str | None, name: str | None = None) -> bool:
    """Best-effort estate-vs-trust decision for the Estateably deep-link segment.

    Prefer the resolved matter type; fall back to the name (Estateably trust names
    contain "Trust", e.g. "Cayden Nichols 2020 Trust").
    """
    if matter_type:
        return matter_type.lower() == "trust"
    return bool(name and "trust" in name.lower())


def quickbooks_customer_url(customer_id: str, *, base_url: str | None = None) -> str:
    """QuickBooks Online customer-detail deep-link (best-effort — VERIFY).

    Sandbox vs production is inferred from the API base URL; the QBO app resolves
    the record within the signed-in company.
    """
    host = _QBO_SANDBOX_HOST if base_url and "sandbox" in base_url else _QBO_HOST
    return f"{host}/app/customerdetail?nameId={customer_id}"
