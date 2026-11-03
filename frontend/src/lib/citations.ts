// Turns the inline "(system:record_id)" markers the synthesis model writes into
// the section prose into numbered footnotes ([1], [2], …) keyed to a references
// list. Numbering is by first appearance across sections; only refs that are
// present in answer.citations (grounded) get a number — ungrounded markers are
// dropped. Any grounded citation never referenced inline is appended so every
// source still appears in the references list.

import type { Answer, Citation } from "../types";
import { SECTION_ORDER } from "../types";
import { toText } from "./richtext";

const SYSTEMS = "estateably|clio|quickbooks|graph|sharepoint|outlook";

// Any parenthesised group that carries at least one "system:" token. Kept
// deliberately loose: the model is inconsistent — it drops the "system:" prefix
// on continuation ids ("(estateably:A; B; C)"), truncates long ids with "…", and
// mixes systems ("(estateably:A; outlook:B)"). Matching the whole group (rather
// than a strict token list) means such markers are always removed from the prose
// instead of leaking as raw text; markerRefs() then salvages the usable ids.
export const CITE_MARKER_RE = new RegExp(`\\([^)]*?(?:${SYSTEMS})\\s*:[^)]*\\)`, "gi");

const _SYS_ONLY_RE = new RegExp(`^(?:${SYSTEMS})$`, "i");

// Pull the (system, id) refs out of one marker, inheriting the last-named system
// for bare continuation ids. Trailing punctuation and empty/garbled parts are
// dropped. Truncated ids (containing "…" or "...") simply won't match a real
// citation later, so they resolve to nothing rather than a broken footnote.
export function markerRefs(marker: string): Array<{ system: string; id: string }> {
  const inner = marker.replace(/^\(|\)$/g, "");
  const refs: Array<{ system: string; id: string }> = [];
  let lastSystem: string | null = null;
  for (const raw of inner.split(";")) {
    const part = raw.trim();
    if (!part) continue;
    let system: string | null = null;
    let rest = part;
    const colon = part.indexOf(":");
    if (colon >= 0 && _SYS_ONLY_RE.test(part.slice(0, colon).trim())) {
      system = part.slice(0, colon).trim().toLowerCase();
      rest = part.slice(colon + 1);
      lastSystem = system;
    } else {
      system = lastSystem; // bare continuation id — inherit the marker's system
    }
    if (!system) continue;
    // The id is the first token; stop at whitespace/comma (the model often trails
    // a date or note inside the parens, e.g. "outlook:AAMk…AAA=, 2026-03-27"), and
    // drop trailing sentence punctuation. Base64 chars (= / +) are kept.
    const id = (rest.trim().match(/^[^\s,;)]+/)?.[0] ?? "").replace(/[.,;]+$/, "");
    if (id) refs.push({ system, id });
  }
  return refs;
}

export interface NumberedCite extends Citation {
  n: number;
}

export interface CitationIndex {
  numByRef: Map<string, number>; // "system:id" -> footnote number
  citByRef: Map<string, Citation>; // "system:id" -> citation (url/note lookup)
  ordered: NumberedCite[]; // references list, in footnote order
}

const refKey = (system: string, id: string) => `${system.toLowerCase()}:${id}`;

// Core: number the inline markers found across `texts`, grounded against
// `citations`. Shared by the full status file (its section texts) and the Tier-0
// quick answer (its single text).
export function buildCitationIndexFromTexts(
  texts: (string | undefined)[],
  citations: Citation[] | undefined,
): CitationIndex {
  const citByRef = new Map<string, Citation>();
  for (const c of citations ?? []) {
    citByRef.set(refKey(c.system, c.record_id), c);
  }

  const numByRef = new Map<string, number>();
  const ordered: NumberedCite[] = [];
  const assign = (key: string) => {
    if (numByRef.has(key)) return;
    const cit = citByRef.get(key);
    if (!cit) return; // ungrounded — drop
    const n = ordered.length + 1;
    numByRef.set(key, n);
    ordered.push({ ...cit, n });
  };

  // Number in first-appearance order across the given texts. Coerce each to a
  // string first: a section value can arrive as an array/object (the model
  // occasionally emits one despite the string contract), and `.matchAll` on a
  // non-string would throw and blank the report before it renders.
  for (const raw of texts) {
    const text = toText(raw);
    if (!text) continue;
    for (const marker of text.matchAll(CITE_MARKER_RE)) {
      for (const r of markerRefs(marker[0])) {
        assign(refKey(r.system, r.id));
      }
    }
  }
  // Append grounded citations never referenced inline.
  for (const c of citations ?? []) {
    assign(refKey(c.system, c.record_id));
  }

  return { numByRef, citByRef, ordered };
}

export function buildCitationIndex(answer: Answer): CitationIndex {
  return buildCitationIndexFromTexts(
    SECTION_ORDER.map(({ key }) => answer.sections?.[key]),
    answer.citations,
  );
}
