"""Retrieval from an Amazon Bedrock Knowledge Base (managed RAG).

The matter-search agent's SharePoint documents come from a Bedrock Knowledge
Base rather than a live Graph search: app-only content search is blocked on HT's
legacy NetDocs-migrated libraries, but app-only *reading* works, so a scheduled
crawler (see ``backend.lambdas.sharepoint_crawler``) copies documents to S3 and a
Knowledge Base indexes their **full body text** into a vector store. At query
time this thin wrapper runs a semantic ``Retrieve`` and returns grounded chunks
with their source metadata, which ``DossierBuilder._collect_sharepoint`` folds
into the ``documents`` dossier section (with SharePoint deep-links + citations).

Only the managed ``Retrieve`` API is used (not ``RetrieveAndGenerate``): the
existing Bedrock synthesis step owns generation + citation grounding, so this
just supplies grounded source material — consistent with every other connector.

Data residency: the Knowledge Base / vector store may live outside Canada (a
documented, client-relaxed cross-border flow); its region is config-driven so the
choice is a one-line change. The boto3 client is injectable for testing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from services.logging import get_logger

logger = get_logger(__name__)

DEFAULT_REGION = "ca-central-1"
DEFAULT_TOP_K = 20


class KnowledgeBaseError(Exception):
    """Raised when a Bedrock Knowledge Base retrieval fails."""


@dataclass(frozen=True)
class KBResult:
    """One retrieved chunk: its text, source and provenance metadata."""

    text: str
    score: float
    uri: str | None  # the S3 object the chunk came from
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def web_url(self) -> str | None:
        """The original SharePoint URL, stashed as metadata by the crawler."""
        v = self.metadata.get("web_url") or self.metadata.get("webUrl")
        return v if isinstance(v, str) else None

    @property
    def name(self) -> str | None:
        v = self.metadata.get("name")
        return v if isinstance(v, str) else None


class KnowledgeBaseClient:
    """Thin Bedrock ``Retrieve`` client for the SharePoint document index.

    Auth relies on the ambient AWS credentials/role (the Lambda execution role in
    production). ``knowledge_base_id`` and ``region`` are config values so the
    deployment can point at the KB wherever it is provisioned without code changes.
    """

    def __init__(
        self,
        knowledge_base_id: str | None = None,
        *,
        region: str | None = None,
        client: Any | None = None,
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        self.knowledge_base_id = knowledge_base_id or os.environ.get("APHRODITE_SHAREPOINT_KB_ID")
        self.region = region or os.environ.get("APHRODITE_KB_REGION") or os.environ.get(
            "APHRODITE_REGION", DEFAULT_REGION
        )
        self._client = client  # lazily created so tests need no AWS/boto3
        self._top_k = top_k

    @property
    def client(self) -> Any:
        if self._client is None:
            import boto3  # imported lazily; not needed when a client is injected

            self._client = boto3.client("bedrock-agent-runtime", region_name=self.region)
        return self._client

    def retrieve(self, query: str, *, top_k: int | None = None) -> list[KBResult]:
        """Return the top matching document chunks for ``query`` (semantic search).

        Raises :class:`KnowledgeBaseError` if the KB id is unset or the call
        fails, so the caller can record it as a soft source failure.
        """
        if not self.knowledge_base_id:
            raise KnowledgeBaseError("no knowledge base id configured")
        k = top_k or self._top_k
        request = {
            "knowledgeBaseId": self.knowledge_base_id,
            "retrievalQuery": {"text": query},
            "retrievalConfiguration": {
                "vectorSearchConfiguration": {"numberOfResults": k}
            },
        }
        try:
            response = self.client.retrieve(**request)
        except Exception as exc:  # noqa: BLE001 - normalize any boto/client error
            raise KnowledgeBaseError(f"Bedrock retrieve failed: {exc}") from exc
        return [_parse_result(r) for r in response.get("retrievalResults", [])]


def _parse_result(row: dict[str, Any]) -> KBResult:
    text = ((row.get("content") or {}).get("text")) or ""
    score = row.get("score")
    location = row.get("location") or {}
    uri = (location.get("s3Location") or {}).get("uri")
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    return KBResult(
        text=str(text),
        score=float(score) if isinstance(score, (int, float)) else 0.0,
        uri=uri if isinstance(uri, str) else None,
        metadata=metadata,
    )
