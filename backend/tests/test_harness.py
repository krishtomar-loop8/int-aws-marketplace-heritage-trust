import pytest

from backend import harness
from backend.harness import lambda_handler, run_checks


def test_run_checks_all_pass():
    report = run_checks({"a": lambda: None, "b": lambda: None})
    assert report["ok"] is True
    assert report["sources"]["a"]["ok"] is True
    assert report["sources"]["b"]["ok"] is True


def test_run_checks_reports_per_source_failure():
    def boom():
        raise RuntimeError("nope")

    report = run_checks({"a": lambda: None, "b": boom})
    assert report["ok"] is False
    assert report["sources"]["a"]["ok"] is True
    assert report["sources"]["b"]["ok"] is False
    assert "nope" in report["sources"]["b"]["detail"]


def test_lambda_handler_delegates(monkeypatch):
    sentinel = {"ok": True, "sources": {}}
    monkeypatch.setattr("backend.harness.run_checks", lambda: sentinel)
    assert lambda_handler() is sentinel


def test_run_check_returns_single_source_result(monkeypatch):
    def fake_probe():
        return None

    monkeypatch.setattr(harness, "_default_probes", lambda secrets: {"estateably": fake_probe})
    result = harness.run_check("estateably", secrets=object())
    assert result == {"ok": True, "detail": "ok"}


def test_run_check_unknown_connector_raises():
    with pytest.raises(ValueError):
        harness.run_check("not-a-connector", secrets=object())
