"""Model-agnostic LLM synthesis via Amazon Bedrock (Converse API).

The Global Matter Search agent's synthesis step calls Claude through this thin
wrapper. It uses Bedrock's **Converse** API, which is model-agnostic (the same
call works across Claude versions and other providers) and returns token usage,
so the concrete model is a config value rather than a hard-coded assumption.

Model selection (Aphrodite context)
----------------------------------------
In ``ca-central-1`` the newest Claude models are **inference-profile-only** and
the only Anthropic profiles are ``us.*`` / ``global.*`` — invoking them routes
inference to a US region (a documented cross-border flow pending compliance
sign-off). Only ``anthropic.claude-3-*`` are on-demand fully in-region. Because
the model id is config-driven, switching between the cross-region newer model
and an in-region fallback is a one-line change (env var or constructor arg):

- default (dev): ``us.anthropic.claude-sonnet-4-6`` called via the
  ``ca-central-1`` Bedrock endpoint (request enters in Canada, inference in US);
- in-region fallback: ``anthropic.claude-3-sonnet-20240229-v1:0``.

Bedrock does not retain prompts/outputs and Anthropic applies zero data
retention on Bedrock; there is no per-request flag for this (it is governed by
the AWS/Anthropic terms), so none is sent here.

The boto3 client and the sleep function are injectable for testing.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from services.logging import get_logger

logger = get_logger(__name__)

# Cross-region newer model by default (see module docstring); override via env or
# constructor. The in-region on-demand fallback is claude-3-sonnet.
DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
DEFAULT_REGION = "ca-central-1"

DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.0

# Bedrock error codes that are transient and worth retrying.
_RETRYABLE_ERRORS = frozenset(
    {
        "ThrottlingException",
        "ServiceUnavailableException",
        "ModelTimeoutException",
        "InternalServerException",
        "ModelNotReadyException",
    }
)


class LLMError(Exception):
    """Raised when a Bedrock call fails (non-retryable, or retries exhausted)."""


@dataclass(frozen=True)
class LLMResult:
    """Result of a synthesis call.

    ``cache_read_tokens`` / ``cache_write_tokens`` come from the Converse ``usage``
    block when a ``cachePoint`` is used (the static framework prefix). Cache reads
    are billed at a fraction of the input rate and cache writes at a premium, so
    they are captured separately for accurate per-search cost accounting.
    """

    text: str
    model_id: str
    input_tokens: int
    output_tokens: int
    stop_reason: str
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0


class BedrockLLM:
    """Model-agnostic Bedrock Converse client.

    Auth relies on the ambient AWS credentials/role (the Lambda execution role in
    production). The model id and region are config values so the deployment can
    switch between the cross-region newer model and the in-region fallback
    without code changes.
    """

    def __init__(
        self,
        *,
        model_id: str | None = None,
        region: str | None = None,
        client: Any | None = None,
        max_retries: int = 4,
        base_delay: float = 1.0,
        read_timeout: int = 150,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.model_id = model_id or os.environ.get("APHRODITE_BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
        self.region = region or os.environ.get("APHRODITE_BEDROCK_REGION", DEFAULT_REGION)
        self._client = client  # lazily created so tests need no AWS/boto3
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._read_timeout = read_timeout
        self._sleep = sleep

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3  # imported lazily; not needed when a client is injected
            from botocore.config import Config

            # A full synthesis can generate for well over boto3's default 60s read
            # timeout; without this the call times out and botocore silently
            # retries it (~3x), turning one slow call into minutes. Use a generous
            # read timeout and disable botocore retries — throttling retries are
            # handled by _converse_with_retries.
            cfg = Config(
                connect_timeout=10,
                read_timeout=self._read_timeout,
                retries={"max_attempts": 1, "mode": "standard"},
            )
            self._client = boto3.client("bedrock-runtime", region_name=self.region, config=cfg)
        return self._client

    def synthesize(
        self,
        prompt: str,
        *,
        system: str | None = None,
        cache_system: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = DEFAULT_TEMPERATURE,
    ) -> LLMResult:
        """Run a single-turn synthesis and return the text plus token usage.

        ``cache_system`` is a large *static* system prefix (e.g. the operational
        framework) marked with a Bedrock **cachePoint** so identical prefixes are
        billed at the cached rate on subsequent calls — the main cost lever for
        this agent. ``system`` is any additional (non-cached) system text.

        Retries transient Bedrock errors (throttling / timeouts) with exponential
        backoff; other errors are wrapped in :class:`LLMError`.
        """
        system_blocks: list[dict[str, Any]] = []
        if cache_system:
            system_blocks.append({"text": cache_system})
            system_blocks.append({"cachePoint": {"type": "default"}})
        if system:
            system_blocks.append({"text": system})

        request: dict[str, Any] = {
            "modelId": self.model_id,
            "messages": [{"role": "user", "content": [{"text": prompt}]}],
            "inferenceConfig": {"maxTokens": max_tokens, "temperature": temperature},
        }
        if system_blocks:
            request["system"] = system_blocks

        response = self._converse_with_retries(request)

        try:
            content = response["output"]["message"]["content"]
            text = "".join(block.get("text", "") for block in content)
            usage = response.get("usage", {})
            return LLMResult(
                text=text,
                model_id=self.model_id,
                input_tokens=int(usage.get("inputTokens", 0)),
                output_tokens=int(usage.get("outputTokens", 0)),
                stop_reason=str(response.get("stopReason", "")),
                cache_read_tokens=int(usage.get("cacheReadInputTokens", 0) or 0),
                cache_write_tokens=int(usage.get("cacheWriteInputTokens", 0) or 0),
            )
        except (KeyError, TypeError) as exc:
            raise LLMError(f"Unexpected Bedrock Converse response shape: {exc}") from exc

    def _converse_with_retries(self, request: dict[str, Any]) -> dict[str, Any]:
        # Lazy imports (only needed at call time). ReadTimeoutError / ConnectionError
        # are BotoCoreError subclasses, NOT ClientError — without catching them they
        # escape unwrapped as a raw "Read timeout on endpoint URL …" string.
        from botocore.exceptions import (
            ClientError,
            ConnectionClosedError,
            ReadTimeoutError,
        )
        from botocore.exceptions import (
            ConnectionError as BotoConnectionError,  # base for connect/endpoint errors
        )

        attempt = 0
        while True:
            try:
                return self.client.converse(**request)
            except ReadTimeoutError as exc:
                # The socket read ran past read_timeout — i.e. generation is taking
                # longer than we allotted. Retrying blocks for read_timeout again and
                # risks the Lambda budget for little gain, so fail cleanly; the caller
                # surfaces a friendly message and the user can retry a fresh run.
                raise LLMError("Bedrock Converse timed out waiting for the response") from exc
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code not in _RETRYABLE_ERRORS or attempt >= self._max_retries:
                    raise LLMError(f"Bedrock Converse failed ({code or 'unknown'})") from exc
                self._backoff(attempt, code)
            except (BotoConnectionError, ConnectionClosedError) as exc:
                # Connection failures fail fast (not after read_timeout), so retrying
                # with backoff is cheap and rides out a transient network blip.
                if attempt >= self._max_retries:
                    raise LLMError(f"Bedrock connection failed ({type(exc).__name__})") from exc
                self._backoff(attempt, type(exc).__name__)
            attempt += 1

    def _backoff(self, attempt: int, code: str) -> None:
        delay = self._base_delay * (2**attempt)
        logger.info(
            "bedrock retryable error; backing off",
            extra={"extra_fields": {"code": code, "attempt": attempt, "delay": delay}},
        )
        self._sleep(delay)
