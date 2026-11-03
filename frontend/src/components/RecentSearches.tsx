import { useState } from "react";
import { timeAgo, useSearchJobs } from "../lib/searchJobs";
import type { SearchJobSummary } from "../types";

// The conversation-history list nested under Matter Search in the sidebar
// (claude.ai style). Shows the 5 most recent by default (CSS-capped height);
// scroll within the same list to reach older ones. In-flight searches show
// live progress; completed ones re-open their stored status file in the chat.
export function RecentSearches() {
  const { searches, openSearch, removeSearch } = useSearchJobs();

  if (searches.length === 0) {
    return <p className="recent-empty">No recent searches yet.</p>;
  }

  return (
    <ul className="recent-list">
      {searches.map((job) => (
        <RecentItem
          key={job.job_id}
          job={job}
          onOpen={() => openSearch(job)}
          onDelete={() => removeSearch(job.job_id)}
        />
      ))}
    </ul>
  );
}

function RecentItem({
  job,
  onOpen,
  onDelete,
}: {
  job: SearchJobSummary;
  onOpen: () => void;
  onDelete: () => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [deleteFailed, setDeleteFailed] = useState(false);
  const label = job.matter_name || job.query || "Search";
  const failed = job.status === "error";
  // Done → re-open the stored status file; Failed → re-run the query. Only a
  // still-running search is non-interactive.
  const clickable = job.status === "done" || failed;

  return (
    <li className="recent-row">
      <button
        className="recent-item"
        onClick={clickable ? onOpen : undefined}
        disabled={!clickable}
        aria-disabled={!clickable}
        title={failed ? `${label} — click to retry` : label}
      >
        <span className="recent-item-title">{label}</span>
        <span className="recent-item-meta">
          {deleteFailed ? (
            <span className="recent-failed">Couldn't delete — try again</span>
          ) : job.status === "pending" ? (
            <>
              <span className="spinner" aria-hidden="true" /> {job.phase || "Running…"}
            </>
          ) : failed ? (
            <span className="recent-failed">Failed · retry</span>
          ) : (
            timeAgo(job.created_at)
          )}
        </span>
      </button>

      {confirming ? (
        <span className="recent-confirm">
          <button
            className="recent-confirm-btn recent-confirm-yes"
            onClick={() => {
              setConfirming(false);
              onDelete().catch(() => {
                setDeleteFailed(true);
                window.setTimeout(() => setDeleteFailed(false), 4000);
              });
            }}
            title="Confirm delete"
            aria-label="Confirm delete"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
              <path d="M5 12l5 5 9-9" />
            </svg>
          </button>
          <button
            className="recent-confirm-btn recent-confirm-no"
            onClick={() => setConfirming(false)}
            title="Cancel"
            aria-label="Cancel delete"
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.8} strokeLinecap="round" strokeLinejoin="round">
              <path d="M6 6l12 12M18 6L6 18" />
            </svg>
          </button>
        </span>
      ) : (
        <button
          className="recent-delete"
          onClick={() => setConfirming(true)}
          title={`Delete "${label}"`}
          aria-label={`Delete "${label}"`}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 7h16M9 7V5a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2m-9 0 1 13a1 1 0 0 0 1 1h8a1 1 0 0 0 1-1l1-13" />
          </svg>
        </button>
      )}
    </li>
  );
}
