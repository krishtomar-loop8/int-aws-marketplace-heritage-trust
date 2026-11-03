// Write a dual clipboard payload: text/html (so Outlook and other rich editors
// paste it formatted) plus text/plain (Markdown-ish, for the internal AI or any
// plain editor). Falls back to plain-text-only where ClipboardItem isn't
// available. Requires a secure context (HTTPS / localhost); throws otherwise so
// the caller can surface an error state.

export async function copyRichText(html: string, plain: string): Promise<void> {
  const clip = navigator.clipboard;
  if (!clip) throw new Error("Clipboard API unavailable");

  if (typeof ClipboardItem !== "undefined" && clip.write) {
    try {
      await clip.write([
        new ClipboardItem({
          "text/html": new Blob([html], { type: "text/html" }),
          "text/plain": new Blob([plain], { type: "text/plain" }),
        }),
      ]);
      return;
    } catch {
      // Fall through to plain text (e.g. browser rejects the html type).
    }
  }
  await clip.writeText(plain);
}
