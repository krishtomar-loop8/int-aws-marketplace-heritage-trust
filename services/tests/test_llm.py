"""Unit tests for the model-agnostic Bedrock LLM wrapper.

No AWS or network: a fake bedrock-runtime client is injected, and retryable
failures are simulated with botocore ClientError. Sleep is a no-op.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from services.llm import (
    DEFAULT_MODEL_ID,
    DEFAULT_REGION,
    BedrockLLM,
    LLMError,
)


def ok_response(
    text: str = "hello", *, input_tokens: int = 10, output_tokens: int = 5, stop: str = "end_turn"
) -> dict[str, Any]:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {"inputTokens": input_tokens, "outputTokens": output_tokens},
        "stopReason": stop,
    }


def client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Converse")


class FakeBedrock:
    """Records converse() calls; replays a programmed sequence of results/errors."""

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    def converse(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def make_llm(results: list[Any], **kwargs: Any) -> tuple[BedrockLLM, FakeBedrock]:
    fake = FakeBedrock(results)
    llm = BedrockLLM(client=fake, sleep=lambda _d: None, **kwargs)
    return llm, fake


# -- request shape + parsing ------------------------------------------------


def test_synthesize_returns_text_and_usage() -> None:
    llm, _ = make_llm([ok_response(text="the answer", input_tokens=42, output_tokens=7)])
    result = llm.synthesize("question")

    assert result.text == "the answer"
    assert result.input_tokens == 42
    assert result.output_tokens == 7
    assert result.stop_reason == "end_turn"
    assert result.model_id == DEFAULT_MODEL_ID
    # Absent cache fields default to 0 (no cachePoint hit).
    assert result.cache_read_tokens == 0
    assert result.cache_write_tokens == 0


def test_synthesize_captures_cache_tokens() -> None:
    resp = ok_response()
    resp["usage"].update({"cacheReadInputTokens": 54_000, "cacheWriteInputTokens": 1_200})
    llm, _ = make_llm([resp])
    result = llm.synthesize("question")
    assert result.cache_read_tokens == 54_000
    assert result.cache_write_tokens == 1_200


def test_request_shape_includes_system_and_inference_config() -> None:
    llm, fake = make_llm([ok_response()], model_id="test-model")
    llm.synthesize("my prompt", system="you are helpful", max_tokens=1234, temperature=0.3)

    req = fake.calls[0]
    assert req["modelId"] == "test-model"
    assert req["messages"] == [{"role": "user", "content": [{"text": "my prompt"}]}]
    assert req["system"] == [{"text": "you are helpful"}]
    assert req["inferenceConfig"] == {"maxTokens": 1234, "temperature": 0.3}


def test_system_omitted_when_not_provided() -> None:
    llm, fake = make_llm([ok_response()])
    llm.synthesize("prompt only")
    assert "system" not in fake.calls[0]


def test_cache_system_adds_cachepoint_before_dynamic_system() -> None:
    llm, fake = make_llm([ok_response()])
    llm.synthesize("q", cache_system="STATIC FRAMEWORK", system="dynamic bit")
    assert fake.calls[0]["system"] == [
        {"text": "STATIC FRAMEWORK"},
        {"cachePoint": {"type": "default"}},
        {"text": "dynamic bit"},
    ]


def test_cache_system_alone_emits_text_then_cachepoint() -> None:
    llm, fake = make_llm([ok_response()])
    llm.synthesize("q", cache_system="STATIC")
    assert fake.calls[0]["system"] == [
        {"text": "STATIC"},
        {"cachePoint": {"type": "default"}},
    ]


def test_multiple_text_blocks_are_concatenated() -> None:
    resp = ok_response()
    resp["output"]["message"]["content"] = [{"text": "part one "}, {"text": "part two"}]
    llm, _ = make_llm([resp])
    assert llm.synthesize("q").text == "part one part two"


def test_unexpected_response_shape_raises_llm_error() -> None:
    llm, _ = make_llm([{"unexpected": "shape"}])
    with pytest.raises(LLMError):
        llm.synthesize("q")


# -- retry behaviour --------------------------------------------------------


def test_retries_transient_error_then_succeeds() -> None:
    llm, fake = make_llm([client_error("ThrottlingException"), ok_response(text="recovered")])
    result = llm.synthesize("q")
    assert result.text == "recovered"
    assert len(fake.calls) == 2  # one failed, one succeeded


def test_non_retryable_error_is_not_retried() -> None:
    llm, fake = make_llm([client_error("ValidationException")])
    with pytest.raises(LLMError):
        llm.synthesize("q")
    assert len(fake.calls) == 1  # no retry


def test_retries_are_bounded_by_max_retries() -> None:
    # max_retries=2 → 1 initial + 2 retries = 3 attempts, all throttled.
    errors = [client_error("ThrottlingException")] * 5
    llm, fake = make_llm(errors, max_retries=2)
    with pytest.raises(LLMError):
        llm.synthesize("q")
    assert len(fake.calls) == 3


def test_read_timeout_is_not_retried_and_raises_clean_error() -> None:
    # A read timeout blocks for the full read_timeout; retrying risks the Lambda
    # budget for little gain, so it fails cleanly on the first attempt.
    llm, fake = make_llm([ReadTimeoutError(endpoint_url="https://bedrock")])
    with pytest.raises(LLMError) as ei:
        llm.synthesize("q")
    assert "timed out" in str(ei.value).lower()
    assert "read timeout on endpoint" not in str(ei.value).lower()  # raw string not leaked
    assert len(fake.calls) == 1  # no retry


def test_connection_error_is_retried_then_succeeds() -> None:
    # Connection failures fail fast, so retrying with backoff rides out a blip.
    llm, fake = make_llm(
        [ConnectTimeoutError(endpoint_url="https://bedrock"), ok_response(text="recovered")]
    )
    result = llm.synthesize("q")
    assert result.text == "recovered"
    assert len(fake.calls) == 2


def test_connection_error_bounded_by_max_retries() -> None:
    errs = [EndpointConnectionError(endpoint_url="https://bedrock")] * 5
    llm, fake = make_llm(errs, max_retries=2)
    with pytest.raises(LLMError):
        llm.synthesize("q")
    assert len(fake.calls) == 3  # 1 initial + 2 retries


# -- config resolution ------------------------------------------------------


def test_defaults_when_no_env_or_args(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APHRODITE_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("APHRODITE_BEDROCK_REGION", raising=False)
    llm = BedrockLLM(client=FakeBedrock([]))
    assert llm.model_id == DEFAULT_MODEL_ID
    assert llm.region == DEFAULT_REGION


def test_env_overrides_default_and_arg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APHRODITE_BEDROCK_MODEL_ID", "env-model")
    monkeypatch.setenv("APHRODITE_BEDROCK_REGION", "env-region")
    assert BedrockLLM(client=FakeBedrock([])).model_id == "env-model"
    assert BedrockLLM(client=FakeBedrock([])).region == "env-region"
    # explicit arg wins over env
    assert BedrockLLM(client=FakeBedrock([]), model_id="arg-model").model_id == "arg-model"
