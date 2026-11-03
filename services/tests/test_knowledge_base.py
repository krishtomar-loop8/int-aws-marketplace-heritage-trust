"""Unit tests for the Bedrock Knowledge Base retrieve wrapper.

No AWS or network: a fake bedrock-agent-runtime client is injected.
"""

from __future__ import annotations

from typing import Any

import pytest

from services.knowledge_base import KnowledgeBaseClient, KnowledgeBaseError


def _hit(text: str, uri: str, score: float, **metadata: Any) -> dict[str, Any]:
    return {
        "content": {"text": text},
        "score": score,
        "location": {"type": "S3", "s3Location": {"uri": uri}},
        "metadata": metadata,
    }


class FakeRuntime:
    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.calls: list[dict[str, Any]] = []

    def retrieve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._response


def test_retrieve_parses_results_and_metadata() -> None:
    fake = FakeRuntime(
        {
            "retrievalResults": [
                _hit(
                    "Grant of probate filed 2024-03-01.",
                    "s3://aphrodite-crawl/ExampleSite/McDonald/probate.pdf",
                    0.82,
                    web_url="https://ht.sharepoint.com/sites/Law/McDonald/probate.pdf",
                    name="probate.pdf",
                ),
            ]
        }
    )
    kb = KnowledgeBaseClient("kb-123", client=fake, top_k=5)
    results = kb.retrieve("McDonald estate probate")

    assert len(results) == 1
    r = results[0]
    assert r.text.startswith("Grant of probate")
    assert r.score == 0.82
    assert r.uri.endswith("probate.pdf")
    assert r.web_url == "https://ht.sharepoint.com/sites/Law/McDonald/probate.pdf"
    assert r.name == "probate.pdf"
    # request carried the KB id + numberOfResults
    call = fake.calls[0]
    assert call["knowledgeBaseId"] == "kb-123"
    assert call["retrievalQuery"] == {"text": "McDonald estate probate"}
    assert call["retrievalConfiguration"]["vectorSearchConfiguration"]["numberOfResults"] == 5


def test_retrieve_tolerates_missing_fields() -> None:
    fake = FakeRuntime({"retrievalResults": [{"content": {}}]})
    kb = KnowledgeBaseClient("kb-1", client=fake)
    (r,) = kb.retrieve("q")
    assert r.text == ""
    assert r.score == 0.0
    assert r.uri is None
    assert r.web_url is None


def test_retrieve_without_kb_id_raises() -> None:
    kb = KnowledgeBaseClient(None, client=FakeRuntime({"retrievalResults": []}))
    with pytest.raises(KnowledgeBaseError):
        kb.retrieve("q")


def test_retrieve_wraps_client_errors() -> None:
    class Boom:
        def retrieve(self, **_: Any) -> dict[str, Any]:
            raise RuntimeError("throttled")

    kb = KnowledgeBaseClient("kb-1", client=Boom())
    with pytest.raises(KnowledgeBaseError):
        kb.retrieve("q")


def test_top_k_override() -> None:
    fake = FakeRuntime({"retrievalResults": []})
    kb = KnowledgeBaseClient("kb-1", client=fake, top_k=20)
    kb.retrieve("q", top_k=3)
    cfg = fake.calls[0]["retrievalConfiguration"]["vectorSearchConfiguration"]
    assert cfg["numberOfResults"] == 3
