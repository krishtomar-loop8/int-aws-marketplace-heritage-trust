"""Unit tests for the search operational framework's org configurability."""

from __future__ import annotations

from backend.search.framework import render_framework


def test_render_framework_default_org():
    text = render_framework()
    assert "Aphrodite's internal matter-status assistant" in text
    assert "545 Clyde Avenue, West Vancouver" in text
    assert "c/o Aphrodite (mailing address)" in text
    assert "Heritage Trust" not in text


def test_render_framework_custom_org():
    text = render_framework(org_name="Acme Trust", office_address="1 Main St")
    assert "Acme Trust's internal matter-status assistant" in text
    assert "c/o Acme Trust (mailing address)" in text
    assert "1 Main St" in text
    assert "Aphrodite" not in text


def test_framework_distinguishes_disabled_sources_from_gaps():
    text = render_framework()
    assert "disabled_sources" in text
