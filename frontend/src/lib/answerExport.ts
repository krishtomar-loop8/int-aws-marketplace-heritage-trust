// Serialize a matter-search response to clipboard-ready text — a rich-text (HTML)
// payload so Outlook pastes it formatted, and a plain-text (Markdown-ish) payload
// for the internal AI / any plain editor. The output mirrors what's rendered on
// screen: same section order + titles, inline "(system:id)" markers converted to
// "[n]" footnotes (ungrounded ones dropped, exactly like RichText), and a
// Sources-reviewed + References list at the end.

import type { Answer, QuickAnswer } from "../types";
import { SECTION_ORDER } from "../types";
import {
  buildCitationIndex,
  buildCitationIndexFromTexts,
  CITE_MARKER_RE,
  markerRefs,
  type CitationIndex,
} from "./citations";
import { parseBlocks } from "./richtext";
import { SYSTEM_LABEL } from "../components/Citation";

export interface ClipboardPayload {
  html: string;
  plain: string;
}

const esc = (s: string): string =>
  s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const escAttr = (s: string): string => esc(s).replace(/"/g, "&quot;");

// Replace each "(system:id)" marker with the "[n]" footnote number(s) of its
// grounded refs; drop ungrounded markers (and the whitespace that preceded them,
// so prose doesn't read "surfaced ." — mirrors RichText.renderInline).
function markersToFootnotes(text: string, index: CitationIndex): string {
  let out = "";
  let last = 0;
  for (const m of text.matchAll(CITE_MARKER_RE)) {
    const start = m.index ?? 0;
    const nums: number[] = [];
    for (const r of markerRefs(m[0])) {
      const n = index.numByRef.get(`${r.system.toLowerCase()}:${r.id}`);
      if (n !== undefined && !nums.includes(n)) nums.push(n);
    }
    const marker = nums.map((n) => `[${n}]`).join("");
    let before = text.slice(last, start);
    if (!marker) before = before.replace(/\s+$/, "");
    out += before + marker;
    last = start + m[0].length;
  }
  return out + text.slice(last);
}

// -- plain text ------------------------------------------------------------

function sectionPlain(text: string, index: CitationIndex): string {
  const lines: string[] = [];
  for (const b of parseBlocks(text)) {
    if (b.type === "p") {
      lines.push(markersToFootnotes(b.items.join(" "), index));
    } else {
      b.items.forEach((it, i) => {
        const prefix = b.type === "ol" ? `${i + 1}. ` : "- ";
        lines.push(prefix + markersToFootnotes(it, index));
      });
    }
    lines.push("");
  }
  return lines.join("\n").trimEnd();
}

// -- html ------------------------------------------------------------------

function inlineHtml(text: string, index: CitationIndex): string {
  // Split on **bold**, escaping the rest; markers are already "[n]" plain text.
  return markersToFootnotes(text, index)
    .split(/(\*\*[^*]+\*\*)/g)
    .map((part) => {
      const b = /^\*\*([^*]+)\*\*$/.exec(part);
      return b ? `<strong>${esc(b[1])}</strong>` : esc(part);
    })
    .join("");
}

function sectionHtml(text: string, index: CitationIndex): string {
  let html = "";
  for (const b of parseBlocks(text)) {
    if (b.type === "p") {
      html += `<p>${inlineHtml(b.items.join(" "), index)}</p>`;
    } else {
      const tag = b.type; // "ul" | "ol"
      html += `<${tag}>${b.items.map((it) => `<li>${inlineHtml(it, index)}</li>`).join("")}</${tag}>`;
    }
  }
  return html;
}

// -- shared document assembly ----------------------------------------------

interface Doc {
  title: string;
  kind: string; // "Status file" | "Quick answer"
  type?: string | null;
  insufficient?: boolean;
  insufficientReason?: string;
  sections: { title: string; text: string }[];
  index: CitationIndex;
}

const INSUFFICIENT_NOTE =
  "Some sources were unavailable for this search, so parts of the picture may be missing; treat unconfirmed items as outstanding.";

function insufficientLine(doc: Doc): string {
  return doc.insufficientReason
    ? `${INSUFFICIENT_NOTE} (${doc.insufficientReason})`
    : INSUFFICIENT_NOTE;
}

function buildPlain(doc: Doc): string {
  const out: string[] = [];
  out.push(doc.type ? `${doc.title} — ${doc.kind} (${doc.type})` : `${doc.title} — ${doc.kind}`);
  out.push("");
  if (doc.insufficient) {
    out.push(`[!] ${insufficientLine(doc)}`, "");
  }
  for (const s of doc.sections) {
    out.push(s.title, sectionPlain(s.text, doc.index), "");
  }
  if (doc.index.ordered.length) {
    out.push("References");
    for (const c of doc.index.ordered) {
      const label = SYSTEM_LABEL[c.system] ?? c.system;
      const note = c.note ? ` — ${c.note}` : "";
      const url = c.url ? ` (${c.url})` : "";
      out.push(`[${c.n}] ${label}${note}${url}`);
    }
    out.push("");
  }
  return out.join("\n").trimEnd() + "\n";
}

function buildHtml(doc: Doc): string {
  const parts: string[] = [];
  parts.push(`<h2>${esc(doc.title)}</h2>`);
  parts.push(`<p><em>${esc(doc.kind)}${doc.type ? ` · ${esc(doc.type)}` : ""}</em></p>`);
  if (doc.insufficient) {
    parts.push(`<p><strong>${esc(insufficientLine(doc))}</strong></p>`);
  }
  for (const s of doc.sections) {
    parts.push(`<h3>${esc(s.title)}</h3>`);
    parts.push(sectionHtml(s.text, doc.index));
  }
  if (doc.index.ordered.length) {
    parts.push("<h3>References</h3>");
    const items = doc.index.ordered.map((c) => {
      const label = esc(SYSTEM_LABEL[c.system] ?? c.system);
      const linked = c.url ? `<a href="${escAttr(c.url)}">${label}</a>` : label;
      const note = c.note ? ` — ${esc(c.note)}` : "";
      return `<li>[${c.n}] ${linked}${note}</li>`;
    });
    parts.push(`<ul>${items.join("")}</ul>`);
  }
  return `<div>${parts.join("")}</div>`;
}

// -- public API ------------------------------------------------------------

export function answerClipboard(
  matterName: string,
  matterType: string | null,
  answer: Answer,
): ClipboardPayload {
  const index = buildCitationIndex(answer);
  // "Sources reviewed" exports in its framework slot (11th, before the suggested
  // next action): the model's prose (if any) followed by the platform's
  // deterministic list — mirroring the on-screen report.
  const sections = SECTION_ORDER.map(({ key, title }) => {
    let text = answer.sections[key] ?? "";
    if (key === "sources_reviewed") {
      const list = (Array.isArray(answer.sources_reviewed) ? answer.sources_reviewed : [])
        .map((s) => `- ${s}`)
        .join("\n");
      text = [text, list].filter(Boolean).join("\n");
    }
    return { title, text };
  }).filter((s): s is { title: string; text: string } => Boolean(s.text));
  const doc: Doc = {
    title: matterName,
    kind: "Status file",
    type: matterType,
    insufficient: answer.insufficient_info,
    insufficientReason: answer.insufficient_reasons?.[0],
    sections,
    index,
  };
  return { html: buildHtml(doc), plain: buildPlain(doc) };
}

export function quickClipboard(
  matterName: string,
  matterType: string | null,
  quick: QuickAnswer,
): ClipboardPayload {
  const index = buildCitationIndexFromTexts([quick.text], quick.citations);
  const doc: Doc = {
    title: matterName,
    kind: "Quick answer",
    type: matterType,
    sections: [{ title: "Answer", text: quick.text }],
    index,
  };
  return { html: buildHtml(doc), plain: buildPlain(doc) };
}
