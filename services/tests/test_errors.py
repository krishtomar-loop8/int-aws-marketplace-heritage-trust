# services/tests/test_errors.py
from services.errors import ErrorClass, classify


def test_classify_empty_detail_is_unknown() -> None:
    result = classify("")
    assert result.error_class == ErrorClass.UNKNOWN

def test_classify_none_detail_is_unknown() -> None:
    result = classify(None)  # type: ignore[arg-type]
    assert result.error_class == ErrorClass.UNKNOWN

def test_classify_quickbooks_token_is_reauthorize() -> None:
    result = classify("HttpError: QuickBooks token refresh failed: 400", integration="quickbooks")
    assert result.error_class == ErrorClass.NEEDS_REAUTH
    assert "re-authorize" in result.message.lower()
    assert "quickbooks" in result.message.lower()

def test_classify_generic_auth_without_integration() -> None:
    result = classify("HttpError: 401 Unauthorized")
    assert result.error_class == ErrorClass.NEEDS_REAUTH
    assert "quickbooks" not in result.message.lower()
    assert "connector" not in result.message.lower()

def test_classify_billing_hold_is_permanent() -> None:
    result = classify("HttpError: 402 Payment Required")
    assert result.error_class == ErrorClass.PERMANENT
    assert "402" in result.message

def test_classify_forbidden_is_permanent() -> None:
    result = classify("HttpError: 403 Forbidden")
    assert result.error_class == ErrorClass.PERMANENT

def test_classify_rate_limit_is_transient() -> None:
    result = classify("HttpError: 429 Too Many Requests")
    assert result.error_class == ErrorClass.TRANSIENT

def test_classify_timeout_is_transient() -> None:
    result = classify("ReadTimeout: read timed out")
    assert result.error_class == ErrorClass.TRANSIENT

def test_classify_server_error_is_transient() -> None:
    result = classify("HttpError: 503 ServiceUnavailable")
    assert result.error_class == ErrorClass.TRANSIENT

def test_classify_unmatched_detail_is_unknown() -> None:
    result = classify("ValueError: something odd")
    assert result.error_class == ErrorClass.UNKNOWN

def test_classify_auth_marker_wins_over_5xx_when_both_present() -> None:
    # Priority order: auth markers are checked first, matching suggest_fix()'s
    # existing order, even when a 5xx code also appears in the same detail.
    result = classify("HttpError: 401 then retried and got 503")
    assert result.error_class == ErrorClass.NEEDS_REAUTH

def test_classify_without_integration_never_mentions_a_specific_connector() -> None:
    for detail in (
        "HttpError: QuickBooks token refresh failed: 400",
        "HttpError: 402 Payment Required",
        "HttpError: 403 Forbidden",
        "HttpError: 429 Too Many Requests",
        "ReadTimeout: read timed out",
        "HttpError: 503 ServiceUnavailable",
        "ValueError: something odd",
    ):
        result = classify(detail)
        assert "quickbooks" not in result.message.lower()
