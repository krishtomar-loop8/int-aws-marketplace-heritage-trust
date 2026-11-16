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


def test_word_boundary_prevents_request_id_false_positive() -> None:
    # "403" appears inside a request id, not as the actual HTTP status.
    result = classify("ValidationException: bad input (RequestId: a403f-01-02)")
    assert result.error_class != ErrorClass.PERMANENT

def test_word_boundary_prevents_matter_number_false_positive() -> None:
    result = classify("ValueError: matter 1500 not found")
    assert result.error_class != ErrorClass.TRANSIENT

def test_word_boundary_still_matches_a_real_403() -> None:
    result = classify("HttpError: 403 Forbidden")
    assert result.error_class == ErrorClass.PERMANENT

def test_word_boundary_still_matches_a_real_401_auth_marker() -> None:
    result = classify("HttpError: 401 Unauthorized")
    assert result.error_class == ErrorClass.NEEDS_REAUTH

def test_url_embedded_digits_do_not_preempt_the_real_status_code() -> None:
    result = classify(
        "HttpError: GET https://app.clio.com/api/v4/matters/401.json failed: 503"
    )
    assert result.error_class == ErrorClass.TRANSIENT  # the real status, 503, wins

def test_generic_message_never_assumes_a_source_system_or_secrets_manager() -> None:
    for detail in (
        "HttpError: 402 Payment Required",
        "HttpError: 403 Forbidden",
        "HttpError: 429 Too Many Requests",
        "ReadTimeout: read timed out",
        "HttpError: 503 ServiceUnavailable",
        "ValueError: something odd",
    ):
        msg = classify(detail).message.lower()
        assert "source system" not in msg
        assert "secrets manager" not in msg
        assert "next check" not in msg


def test_bedrock_access_denied_names_the_bedrock_console() -> None:
    result = classify("LLMError: Bedrock Converse failed (AccessDeniedException)")
    assert result.error_class == ErrorClass.PERMANENT
    assert "bedrock console" in result.message.lower()
    assert "model access" in result.message.lower()

def test_bedrock_throttling_is_transient_and_never_cites_http_429() -> None:
    result = classify("LLMError: Bedrock Converse failed (ThrottlingException)")
    assert result.error_class == ErrorClass.TRANSIENT
    assert "429" not in result.message
    assert "ai" in result.message.lower() or "model" in result.message.lower()

def test_bedrock_service_unavailable_is_transient() -> None:
    result = classify("LLMError: Bedrock Converse failed (ServiceUnavailableException)")
    assert result.error_class == ErrorClass.TRANSIENT

def test_bedrock_timeout_is_transient() -> None:
    result = classify("LLMError: Bedrock Converse timed out waiting for the response")
    assert result.error_class == ErrorClass.TRANSIENT

def test_bedrock_connection_failure_is_transient() -> None:
    result = classify("LLMError: Bedrock connection failed (ReadTimeoutError)")
    assert result.error_class == ErrorClass.TRANSIENT

def test_bedrock_unrecognized_code_is_bedrock_flavored_unknown() -> None:
    result = classify("LLMError: Bedrock Converse failed (ValidationException)")
    assert result.error_class == ErrorClass.UNKNOWN
    assert "connector" not in result.message.lower()  # not the connector-flavored default
    assert "ai" in result.message.lower() or "model" in result.message.lower()

def test_bedrock_response_shape_error_is_bedrock_flavored_unknown() -> None:
    result = classify("LLMError: Unexpected Bedrock Converse response shape: KeyError('output')")
    assert result.error_class == ErrorClass.UNKNOWN

def test_bedrock_branch_never_fires_for_a_named_connector_caller() -> None:
    # Sanity: a Bedrock marker in a connector-named call should not exist in
    # practice, but confirm the branch is keyed off the detail text, not the
    # integration argument, and doesn't accidentally change named output when
    # no Bedrock marker is present.
    result = classify("HttpError: 403 Forbidden", integration="clio")
    assert result.error_class == ErrorClass.PERMANENT
    assert "bedrock" not in result.message.lower()
