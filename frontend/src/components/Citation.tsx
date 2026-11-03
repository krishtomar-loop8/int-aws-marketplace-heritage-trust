import type { Citation } from "../types";

export const SYSTEM_LABEL: Record<string, string> = {
  estateably: "Estateably",
  clio: "Clio",
  quickbooks: "QuickBooks",
  graph: "M365",
  sharepoint: "SharePoint",
  outlook: "Outlook",
};

// The signature element: a monospace "seal" tying a fact back to a source record.
// When the source record has a URL, the seal becomes a link that opens it.
export function CitationChip({ c }: { c: Citation }) {
  const label = SYSTEM_LABEL[c.system] ?? c.system;
  const title = c.note ?? `${label} record ${c.record_id}`;
  const inner = (
    <>
      <span className="cite-sys">{label}</span>
      <span className="cite-id">{c.record_id}</span>
    </>
  );
  if (c.url) {
    return (
      <a className="cite cite-link" href={c.url} target="_blank" rel="noopener noreferrer" title={title}>
        {inner}
      </a>
    );
  }
  return (
    <span className="cite" title={title}>
      {inner}
    </span>
  );
}
