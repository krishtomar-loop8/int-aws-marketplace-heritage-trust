# SharePoint crawler (→ S3 → Bedrock Knowledge Base)

Scheduled Lambda that indexes a customer's SharePoint documents for the
matter-search agent's **full-body** document retrieval.

## Why a crawler (and not live search)
App-only content **search** is unreliable on some SharePoint tenants, especially
legacy migrated document libraries (Graph drive-search 500s; Search APIs need
user context; CAML exceeds the 5,000 list-view threshold). App-only **reading** —
drive enumeration + file download —
works. So we crawl the granted sites incrementally, copy documents to S3, and let
a Bedrock Knowledge Base extract + embed their text. The search Lambda then does a
semantic `Retrieve` at query time (see `services/knowledge_base.py` and
`DossierBuilder._collect_sharepoint`).

## Flow
1. For each site in `APHRODITE_SHAREPOINT_SITES`, enumerate the drive via Graph `/delta`
   from the saved cursor (`_state/<site>.deltalink` in the bucket) — incremental.
2. Skip folders; drop **HR/Finance** content (`backend/search/scoping.py`); skip
   unsupported types / oversize files; delete tombstoned items.
3. Download each changed file → `documents/<site>/<item-id>/<name>` with a
   `<name>.metadata.json` sidecar carrying the SharePoint `web_url`, name, and
   last-modified (returned in `Retrieve` results for deep-links).
4. If anything changed, trigger `bedrock-agent:StartIngestionJob` on the KB.

Read-only against SharePoint; the delta cursor only advances after a site's items
are processed, so a failed run safely re-reads from the same cursor.

## Env
`APHRODITE_REGION`, `APHRODITE_SHAREPOINT_SITES` (JSON list), `APHRODITE_CRAWL_BUCKET`,
`APHRODITE_CRAWL_KMS_KEY_ID` (optional), `APHRODITE_SHAREPOINT_KB_ID` /
`APHRODITE_SHAREPOINT_DATA_SOURCE_ID` (optional, to re-ingest), `APHRODITE_KB_REGION` (optional).

## Tests
`crawl.py` and `storage.py` are fully unit-tested with fakes (no AWS/network):
`backend/lambdas/sharepoint_crawler/tests/`.
