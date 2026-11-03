import { useCallback, useEffect, useRef, useState } from "react";
import { friendlyError, getConnectorHealth, getSearch, search } from "../api";
import { useSearchJobs } from "../lib/searchJobs";
import { clearThread, loadThread, saveThread } from "../lib/threadStore";
import { MOCK_ANSWER, MOCK_DISAMBIGUATION, MOCK_RESULT } from "../mock";
import type { Candidate, MatterSelection, Message, SearchResult, SearchScope } from "../types";
import { Disambiguation } from "./Disambiguation";
import { QuickAnswer } from "./QuickAnswer";
import { StatusReport } from "./StatusReport";

// The conversation thread lives in the message array and is persisted to
// sessionStorage (see lib/threadStore) so it survives refresh and module
// switching. Follow-up *context* and history/resume remain a later enhancement.
const EXAMPLES = [
  "What's the status on the Smith estate?",
  "Is the Lee matter ready for distribution?",
  "KYC status for the Brown Trust",
];

export function MatterSearchChat({
  apiBase,
  token,
  mock,
}: {
  apiBase: string;
  token?: string;
  mock: boolean;
}) {
  const [messages, setMessages] = useState<Message[]>(() => loadThread());
  const [draft, setDraft] = useState("");
  const [scope, setScope] = useState<SearchScope>("all");
  const [busy, setBusy] = useState(false);
  const [sourceErrors, setSourceErrors] = useState<string[]>([]);
  const [healthDismissed, setHealthDismissed] = useState(false);
  const threadEnd = useRef<HTMLDivElement>(null);
  const { openJob, clearOpen, refresh } = useSearchJobs();

  // Standing connector-health notification: pull the source errors from the
  // caller's most recent full search (e.g. a dead QuickBooks token) so a silent
  // degradation surfaces in-app. Refreshed on mount and after each search.
  const refreshHealth = useCallback(() => {
    if (mock || !token) return;
    getConnectorHealth(apiBase, token).then(setSourceErrors);
  }, [apiBase, token, mock]);
  useEffect(() => {
    refreshHealth();
  }, [refreshHealth]);

  // Keep the newest turn in view as the thread grows.
  useEffect(() => {
    threadEnd.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages]);

  // Re-open a past search from the sidebar list: load its *stored* status file
  // (a quick GET — not a re-run) and show it. openJob carries the query so this
  // doesn't depend on `searches` (which changes every poll and would re-fire the
  // effect); clearOpen() runs in finally, AFTER setMessages, so flipping openJob
  // can't cancel the in-flight fetch via the cleanup.
  useEffect(() => {
    if (!openJob) return;
    const jobId = openJob.job_id;
    const q = openJob.query || openJob.matter_name || "Search";
    // A failed search has no stored result to load — start a fresh thread and
    // re-run the query instead of opening (matches how a done search re-opens).
    if (openJob.status === "error") {
      clearOpen();
      setMessages([]);
      void run(q);
      return;
    }
    let alive = true;
    setBusy(true);
    setMessages([
      { role: "user", text: q },
      { role: "assistant", kind: "loading", phase: "Loading the saved status file" },
    ]);
    getSearch(apiBase, jobId, token)
      .then((result) => {
        if (alive) {
          setMessages([
            { role: "user", text: q },
            { role: "assistant", kind: "result", result, query: q },
          ]);
        }
      })
      .catch((e) => {
        if (alive) {
          const msg = e instanceof Error ? e.message : "Could not load that search";
          setMessages([{ role: "user", text: q }, { role: "assistant", kind: "error", text: msg }]);
        }
      })
      .finally(() => {
        if (alive) setBusy(false);
        clearOpen();
      });
    return () => {
      alive = false;
    };
  }, [openJob, apiBase, token, clearOpen]);

  // Persist the thread once settled — gating on !busy keeps the transient
  // "loading" turn out of storage, so a mid-search refresh restores the last
  // completed thread rather than a stuck spinner or orphan user bubble.
  useEffect(() => {
    if (!busy) saveThread(messages);
  }, [messages, busy]);

  async function run(
    q: string,
    selection?: MatterSelection,
    opts?: { full?: boolean; scope?: SearchScope },
  ) {
    const text = q.trim();
    if (!text || busy) return;
    const full = opts?.full ?? false;
    // The scope toggle applies to the initial resolve; a disambiguation pick sends
    // refs directly (bypassing resolution), so opts.scope overrides only on retry.
    const searchScope = opts?.scope ?? scope;
    setDraft("");
    setBusy(true);
    setMessages((m) => [...m, { role: "user", text }, { role: "assistant", kind: "loading" }]);
    const onProgress = (phase: string) => setMessages((m) => setLoadingPhase(m, phase));
    try {
      let result: SearchResult;
      if (mock) {
        const narrow =
          /balance|how much|who are|beneficiar|kyc|deadline|document|invoice|\bbill|email|correspondence|\bt3\b|\bt1\b|date of birth|\bdob\b|born|date of death|\bdie|marital|province/i.test(
            text,
          ) && !/status|summary|overview/i.test(text);
        if (!full && !selection && narrow) {
          await new Promise((r) => setTimeout(r, 600));
          result = MOCK_ANSWER; // Tier-0 quick answer (fast, no phases)
        } else if (!selection && /lee/i.test(text)) {
          await new Promise((r) => setTimeout(r, 600));
          result = MOCK_DISAMBIGUATION;
        } else {
          // Simulate the pipeline phases so mock mode demos the progress indicator.
          for (const p of [
            "Finding the matter",
            "Reading Estateably, Clio and Outlook",
            "Drafting the source-cited summary",
          ]) {
            onProgress(p);
            await new Promise((r) => setTimeout(r, 650));
          }
          result = MOCK_RESULT;
        }
      } else {
        result = await search(apiBase, text, token, selection, full, searchScope, onProgress);
      }
      setMessages((m) => replaceLast(m, { role: "assistant", kind: "result", result, query: text }));
    } catch (e) {
      const msg = friendlyError(e instanceof Error ? e.message : "");
      setMessages((m) =>
        replaceLast(m, {
          role: "assistant",
          kind: "error",
          text: msg,
          query: text,
          selection,
          full,
          scope: searchScope,
        }),
      );
    } finally {
      setBusy(false);
      refresh(); // surface the finished search in the bell + History right away
      refreshHealth(); // update the connector-health banner from this search
    }
  }

  const started = messages.length > 0;

  return (
    <div className="chat">
      <div className="chat-head">
        <h1 className="chat-title">Matter Search</h1>
        {started && (
          <button
            className="link-btn ghost"
            onClick={() => {
              setMessages([]);
              clearThread();
            }}
            disabled={busy}
          >
            New chat
          </button>
        )}
      </div>

      {sourceErrors.length > 0 && !healthDismissed && (
        <div className="banner banner-warn connector-health">
          <span>
            ⚠ Some sources were unavailable in your most recent search ({sourceErrors.join("; ")}).
            Results may be missing that data until the connection is restored.
          </span>
          <button
            className="link-btn ghost"
            type="button"
            onClick={() => setHealthDismissed(true)}
          >
            Dismiss
          </button>
        </div>
      )}

      <div className="thread" role="log" aria-live="polite">
        {!started && (
          <div className="hero">
            <p className="hero-lede">
              Ask about any matter and get a source-cited status file across Estateably, Clio and
              Outlook — read-only, every fact traceable to its record.
            </p>
            <div className="examples">
              {EXAMPLES.map((ex) => (
                <button key={ex} className="chip" onClick={() => run(ex)}>
                  {ex}
                </button>
              ))}
            </div>
          </div>
        )}

        {messages.map((m, i) =>
          m.role === "user" ? (
            <div className="turn turn-user" key={i}>
              <div className="bubble">{m.text}</div>
            </div>
          ) : (
            <div className="turn turn-assistant" key={i}>
              <AssistantTurn
                message={m}
                onPick={(c) =>
                  run(c.name, { name: c.name, matter_type: c.matter_type, refs: c.refs })
                }
                onViewFull={(query) => run(query, undefined, { full: true })}
                onRetry={
                  m.kind === "error" && m.query
                    ? () => run(m.query!, m.selection, { full: m.full, scope: m.scope })
                    : undefined
                }
              />
            </div>
          ),
        )}
        <div ref={threadEnd} />
      </div>

      <form
        className="composer"
        onSubmit={(e) => {
          e.preventDefault();
          run(draft);
        }}
      >
        <div className="composer-row">
          <textarea
            className="composer-input"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                run(draft);
              }
            }}
            placeholder="Ask about a matter — e.g. What's the status on the Smith estate?"
            aria-label="Matter query"
            rows={1}
            autoFocus
          />
          <span
            className="skip-switch"
            title="Skip Estateably — for older matters that exist in Clio + SharePoint but not Estateably"
          >
            <span className="skip-switch-text" id="skip-estateably-label">
              Skip Estateably
            </span>
            <button
              type="button"
              role="switch"
              aria-checked={scope === "clio"}
              aria-labelledby="skip-estateably-label"
              className={`switch${scope === "clio" ? " is-on" : ""}`}
              onClick={() => setScope((s) => (s === "clio" ? "all" : "clio"))}
            >
              <span className="switch-knob" aria-hidden="true" />
            </button>
          </span>
          <button className="composer-btn" type="submit" disabled={busy || !draft.trim()}>
            {busy ? <span className="spinner" aria-hidden="true" /> : "Send"}
          </button>
        </div>
      </form>
      <p className="composer-disclaimer">
        AI can provide inaccurate information — please verify against the original sources before
        making any decisions.
      </p>
    </div>
  );
}

function AssistantTurn({
  message,
  onPick,
  onViewFull,
  onRetry,
}: {
  message: Extract<Message, { role: "assistant" }>;
  onPick: (c: Candidate) => void;
  onViewFull: (query: string) => void;
  onRetry?: () => void;
}) {
  if (message.kind === "loading") {
    return (
      <div className="status-note">
        <span className="spinner" aria-hidden="true" />
        {message.phase
          ? `${message.phase}…`
          : "Reading the file across systems and drafting a cited summary…"}
      </div>
    );
  }
  if (message.kind === "error") {
    return (
      <div className="banner banner-error report-error">
        <span>{message.text}</span>
        {onRetry && (
          <button type="button" className="retry-btn" onClick={onRetry}>
            Retry
          </button>
        )}
      </div>
    );
  }

  const result = message.result;
  if (result.status === "not_found") {
    return (
      <p className="banner banner-warn">
        No matter matched “{result.query}”. Try the client's surname, or check the spelling.
      </p>
    );
  }
  if (result.status === "disambiguation") {
    return <Disambiguation candidates={result.candidates ?? []} onPick={onPick} />;
  }
  if (result.status === "answer" && result.matter && result.quick) {
    return (
      <QuickAnswer
        matterName={result.matter.name}
        matterType={result.matter.type}
        quick={result.quick}
        onViewFull={() => onViewFull(message.query ?? result.matter!.name)}
      />
    );
  }
  if (result.status === "ok" && result.matter && result.answer) {
    return (
      <>
        <StatusReport
          matterName={result.matter.name}
          matterType={result.matter.type}
          answer={result.answer}
        />
        {result.dossier_errors && result.dossier_errors.length > 0 && (
          <p className="banner banner-warn sources-unavailable">
            Some sources were unavailable for this search, so parts of the picture may be missing:{" "}
            {result.dossier_errors.join("; ")}
          </p>
        )}
        {result.usage && (
          <p className="usage">
            {result.usage.model} · {result.usage.input_tokens + result.usage.output_tokens} tokens
          </p>
        )}
      </>
    );
  }
  return null;
}

function replaceLast(messages: Message[], next: Message): Message[] {
  const copy = messages.slice(0, -1);
  copy.push(next);
  return copy;
}

// Update the in-flight loading turn's phase (no-op if the last turn isn't loading).
function setLoadingPhase(messages: Message[], phase: string): Message[] {
  const last = messages[messages.length - 1];
  if (!last || last.role !== "assistant" || last.kind !== "loading") return messages;
  return [...messages.slice(0, -1), { ...last, phase }];
}
