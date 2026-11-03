"""Deposit generated quarterly drafts into the SharePoint review folder.

Canonical per-quarter layout under Nicole's ``Quarterly Reports`` folder::

    Quarterly Reports/<YYYY-Qn>/<Client — matter#>/Cover Letter.docx
                                                  .../Trust Ledger.pdf

Re-generating a matter overwrites its files (the immutable dated history lives in
S3). Delivery is best-effort and isolated per matter — a SharePoint failure never
discards the S3 draft. Only reached on scheduled runs (the handler gates it).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.connectors.graph.publisher import SharePointPublisher, sanitize_name

from .model import ReportModel

_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
_PDF_MIME = "application/pdf"


def matter_folder_name(model: ReportModel) -> str:
    """``<Client display name> — <matter#>`` (falls back when the client name is absent)."""
    client = model.recipients[0].name if model.recipients else ""
    label = client or model.trust_name or model.matter_number
    return sanitize_name(f"{label} — {model.matter_number}")


@dataclass
class SharePointDelivery:
    """Uploads a matter's drafts under the per-quarter folder, creating folders as needed."""

    publisher: SharePointPublisher
    site_id: str
    root_item_id: str  # the "Quarterly Reports" folder
    _period_folders: dict[str, str] = field(default_factory=dict)  # period.code -> item id

    def _period_folder_id(self, period_code: str) -> str:
        if period_code not in self._period_folders:
            folder = self.publisher.ensure_child_folder(
                self.site_id, self.root_item_id, period_code
            )
            self._period_folders[period_code] = folder["id"]
        return self._period_folders[period_code]

    def deliver(self, model: ReportModel, docx: bytes, pdf: bytes) -> str:
        """Upload the cover letter + trust ledger; return the matter folder's web URL."""
        period_id = self._period_folder_id(model.period.code)
        folder = self.publisher.ensure_child_folder(
            self.site_id, period_id, matter_folder_name(model)
        )
        parent = folder["id"]
        self.publisher.upload_file(self.site_id, parent, "Cover Letter.docx", docx, _DOCX_MIME)
        self.publisher.upload_file(self.site_id, parent, "Trust Ledger.pdf", pdf, _PDF_MIME)
        return folder.get("webUrl", "")
