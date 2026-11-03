import { useEffect, useRef, useState } from "react";
import type { ClipboardPayload } from "../lib/answerExport";
import { copyRichText } from "../lib/clipboard";

// A small icon button that copies a rich-text + plain-text payload to the
// clipboard and shows transient "Copied" / "Copy failed" feedback (~2s). The
// payload is built on click via getContent so nothing is serialized until used.

type State = "idle" | "copied" | "error";

const COPY_ICON = (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <rect x="9" y="9" width="11" height="11" rx="2" />
    <path d="M5 15V5a2 2 0 0 1 2-2h10" />
  </svg>
);

const CHECK_ICON = (
  <svg viewBox="0 0 24 24" aria-hidden="true">
    <path d="M20 6 9 17l-5-5" />
  </svg>
);

export function CopyButton({
  getContent,
  label = "Copy",
  title = "Copy this response — formatted for Outlook, plain text for other tools",
}: {
  getContent: () => ClipboardPayload;
  label?: string;
  title?: string;
}) {
  const [state, setState] = useState<State>("idle");
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => () => window.clearTimeout(timer.current), []);

  const onClick = async () => {
    try {
      const { html, plain } = getContent();
      await copyRichText(html, plain);
      setState("copied");
    } catch {
      setState("error");
    }
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setState("idle"), 2000);
  };

  const cls =
    state === "copied" ? "copy-btn is-copied" : state === "error" ? "copy-btn is-error" : "copy-btn";
  const text = state === "copied" ? "Copied" : state === "error" ? "Copy failed" : label;

  return (
    <button type="button" className={cls} onClick={onClick} aria-label={label} title={title}>
      {state === "copied" ? CHECK_ICON : COPY_ICON}
      <span className="copy-label" aria-live="polite">
        {text}
      </span>
    </button>
  );
}
