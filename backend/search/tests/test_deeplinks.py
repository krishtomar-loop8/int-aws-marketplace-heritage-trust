"""Unit tests for source-record deep-link builders."""

from __future__ import annotations

from backend.search.deeplinks import (
    clio_matter_url,
    estateably_estate_url,
    quickbooks_customer_url,
)


def test_clio_matter_url_uses_base_origin():
    assert (
        clio_matter_url("https://app.clio.com/api/v4", "123")
        == "https://app.clio.com/nc/#/matters/123"
    )


def test_clio_matter_url_respects_region_from_base():
    # A CA-pod base URL keeps the deep-link on the same host.
    assert (
        clio_matter_url("https://eu.app.clio.com/api/v4", "9")
        == "https://eu.app.clio.com/nc/#/matters/9"
    )


def test_clio_matter_url_falls_back_to_default_host():
    assert clio_matter_url(None, "5") == "https://app.clio.com/nc/#/matters/5"


def test_estateably_estate_url():
    base = "https://example.app.estateably.com"
    assert estateably_estate_url(base, "E1") == f"{base}/#/estates/E1/overview"
    # trusts live under a different path segment
    assert estateably_estate_url(base, "T9", is_trust=True) == f"{base}/#/trusts/T9/overview"
    assert estateably_estate_url(None, "E2").endswith("/#/estates/E2/overview")


def test_estateably_is_trust():
    from backend.search.deeplinks import estateably_is_trust

    assert estateably_is_trust("trust") is True
    assert estateably_is_trust("estate") is False
    # fall back to the name when the type is unknown
    assert estateably_is_trust(None, "Cayden Nichols 2020 Trust") is True
    assert estateably_is_trust(None, "Estate of Oonagh McDonald") is False
    assert estateably_is_trust(None, None) is False


def test_quickbooks_customer_url_prod_and_sandbox():
    assert (
        quickbooks_customer_url("77", base_url="https://quickbooks.api.intuit.com")
        == "https://app.qbo.intuit.com/app/customerdetail?nameId=77"
    )
    assert quickbooks_customer_url(
        "77", base_url="https://sandbox-quickbooks.api.intuit.com"
    ).startswith("https://app.sandbox.qbo.intuit.com/")
