// Shared, dependency-free block parser for the synthesis output's Markdown subset:
// paragraphs, "-"/"*"/"•" bullet lists, and "1."/"1)" numbered lists. Used by both
// the on-screen renderer (components/RichText) and the clipboard exporter
// (lib/answerExport) so the two never diverge.

export type Block = { type: "p" | "ul" | "ol"; items: string[] };

const BULLET_RE = /^\s*[-*•]\s+(.*)$/;
const ORDERED_RE = /^\s*\d+[.)]\s+(.*)$/;

// Coerce a value to a string for rendering. Section values are declared as strings,
// but the synthesis model occasionally emits one as an array/object; without this,
// a non-string reaches `.split(...)` below and throws — and with no error boundary
// above, that blanks the whole page. An array becomes newline-joined lines.
export function toText(value: unknown): string {
  if (typeof value === "string") return value;
  if (value == null) return "";
  if (Array.isArray(value)) return value.map((v) => (typeof v === "string" ? v : String(v))).join("\n");
  return String(value);
}

export function parseBlocks(text: unknown): Block[] {
  const blocks: Block[] = [];
  let cur: Block | null = null;
  const start = (type: Block["type"]): Block => {
    if (cur && cur.type === type) return cur;
    if (cur) blocks.push(cur);
    const b: Block = { type, items: [] };
    cur = b;
    return b;
  };
  for (const raw of toText(text).split(/\r?\n/)) {
    const line = raw.trimEnd();
    if (!line.trim()) {
      if (cur) blocks.push(cur);
      cur = null;
      continue;
    }
    const bullet = BULLET_RE.exec(line);
    const ordered = ORDERED_RE.exec(line);
    if (bullet) start("ul").items.push(bullet[1]);
    else if (ordered) start("ol").items.push(ordered[1]);
    else start("p").items.push(line);
  }
  if (cur) blocks.push(cur);
  return blocks;
}
