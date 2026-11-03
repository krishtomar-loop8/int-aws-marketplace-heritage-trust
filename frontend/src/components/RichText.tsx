import type { ReactNode } from "react";
import { CITE_MARKER_RE, markerRefs, type CitationIndex } from "../lib/citations";
import { parseBlocks } from "../lib/richtext";

// A small, dependency-free Markdown subset for the synthesis output: paragraphs,
// "-"/"*"/"•" bullet lists, "1."/"1)" numbered lists, **bold**, and the inline
// "(system:id)" citation markers rendered as numbered footnote links. The block
// parser is shared with the clipboard exporter (lib/richtext).

function renderMarker(marker: string, index: CitationIndex, keyBase: string): ReactNode[] {
  const nodes: ReactNode[] = [];
  let i = 0;
  for (const r of markerRefs(marker)) {
    const key = `${r.system}:${r.id}`;
    const n = index.numByRef.get(key);
    if (n === undefined) continue; // ungrounded — drop
    const cit = index.citByRef.get(key);
    // Outlook deep-links only open for someone with access to that message's mailbox, so an
    // Outlook marker points at its in-app reference (which reveals the email excerpt for
    // verification); other systems keep their working source deep-link.
    const url = cit?.url;
    const inApp = r.system === "outlook" || !url;
    nodes.push(
      <a
        key={`${keyBase}-${i++}`}
        className="cite-ref"
        href={inApp ? `#ref-${n}` : url}
        title={cit?.note ?? undefined}
        {...(inApp ? {} : { target: "_blank", rel: "noopener noreferrer" })}
      >
        [{n}]
      </a>,
    );
  }
  return nodes;
}

function renderText(text: string, keyBase: string): ReactNode[] {
  // Split on **bold** spans, keeping plain text as bare strings.
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) => {
    const bold = /^\*\*([^*]+)\*\*$/.exec(part);
    return bold ? <strong key={`${keyBase}-b${i}`}>{bold[1]}</strong> : part;
  });
}

function renderInline(text: string, index: CitationIndex, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  let last = 0;
  let seg = 0;
  for (const m of text.matchAll(CITE_MARKER_RE)) {
    const start = m.index ?? 0;
    const markerNodes = renderMarker(m[0], index, `${keyBase}-m${seg}`);
    let before = text.slice(last, start);
    // A marker that grounds to nothing is dropped entirely; trim the space that
    // preceded it so we don't leave "surfaced ." or a double space behind.
    if (markerNodes.length === 0) before = before.replace(/\s+$/, "");
    if (before) out.push(...renderText(before, `${keyBase}-s${seg}`));
    out.push(...markerNodes);
    last = start + m[0].length;
    seg++;
  }
  const tail = text.slice(last);
  if (tail) out.push(...renderText(tail, `${keyBase}-s${seg}`));
  return out;
}

export function RichText({ text, index }: { text: string; index: CitationIndex }) {
  const blocks = parseBlocks(text);
  return (
    <>
      {blocks.map((b, i) => {
        if (b.type === "p") {
          return (
            <p className="rt-p" key={i}>
              {renderInline(b.items.join(" "), index, `b${i}`)}
            </p>
          );
        }
        const Tag = b.type; // "ul" | "ol"
        return (
          <Tag className="rt-list" key={i}>
            {b.items.map((it, j) => (
              <li key={j}>{renderInline(it, index, `b${i}-${j}`)}</li>
            ))}
          </Tag>
        );
      })}
    </>
  );
}
