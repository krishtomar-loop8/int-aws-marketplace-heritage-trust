// Session-scoped persistence for the Matter Search thread, so the conversation
// survives a page refresh and switching modules (the component unmounts on tab
// change). sessionStorage — not localStorage — is deliberate: the thread holds
// client matter details (PII), so it must clear when the tab closes and on Sign
// out, and never linger at rest between sessions on shared/managed terminals.

import type { Message } from "../types";

const KEY = "ht.matterSearch.v1";

// The in-flight "loading" turn is transient — never persist or rehydrate it.
const settled = (messages: Message[]): Message[] =>
  messages.filter((m) => !(m.role === "assistant" && m.kind === "loading"));

export function loadThread(): Message[] {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    return Array.isArray(parsed) ? settled(parsed as Message[]) : [];
  } catch {
    return [];
  }
}

export function saveThread(messages: Message[]): void {
  try {
    const keep = settled(messages);
    if (keep.length) sessionStorage.setItem(KEY, JSON.stringify(keep));
    else sessionStorage.removeItem(KEY);
  } catch {
    // Quota or serialization failure — degrade gracefully (thread just isn't persisted).
  }
}

export function clearThread(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    // ignore
  }
}
