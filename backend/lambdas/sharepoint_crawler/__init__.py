"""Scheduled SharePoint → S3 crawler that feeds the document Knowledge Base.

App-only content *search* is blocked on HT's legacy NetDocs-migrated libraries,
but app-only *reading* (drive enumeration + file download) works. This Lambda
walks the granted sites' drives via Graph ``/delta`` (incremental), copies the
supported documents to an S3 bucket with a metadata sidecar (SharePoint deep-link,
name, last-modified), excludes HR/Finance content, and triggers a Bedrock
Knowledge Base ingestion so the documents' full body text is indexed for the
matter-search agent's semantic retrieval.
"""
