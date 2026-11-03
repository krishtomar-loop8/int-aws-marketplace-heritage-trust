// Global search-jobs tracker. Searches run server-side to completion, so this
// polls the caller's job list above the tab switch — surviving navigation away
// from Matter Search — to drive the recent-searches list in the sidebar, the
// "finished" indicator on the Matter Search nav item, and re-opening a past
// search's stored status file.

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import type { ReactNode } from "react";
import { deleteSearch, listSearches } from "../api";
import type { SearchJobSummary } from "../types";

const POLL_MS = 8000;

const isTerminal = (s: SearchJobSummary) => s.status === "done" || s.status === "error";

interface SearchJobsValue {
  searches: SearchJobSummary[];
  refresh: () => void;
  openJob: SearchJobSummary | null; // a search the user asked to re-open
  openSearch: (job: SearchJobSummary) => void;
  clearOpen: () => void;
  removeSearch: (jobId: string) => Promise<void>;
  unread: number; // searches that finished while away from Matter Search
  markSeen: () => void;
}

const Ctx = createContext<SearchJobsValue | null>(null);

export function SearchJobsProvider({
  apiBase,
  token,
  mock,
  children,
}: {
  apiBase: string;
  token?: string;
  mock: boolean;
  children: ReactNode;
}) {
  const [searches, setSearches] = useState<SearchJobSummary[]>([]);
  const [openJob, setOpenJob] = useState<SearchJobSummary | null>(null);
  const [unread, setUnread] = useState(0);
  const ackRef = useRef<Set<string>>(new Set()); // terminal job ids already seen
  const seededRef = useRef(false); // seeded on the first fetch of the session

  // Session-scoped "finished" count: everything already complete at app-open is
  // pre-acknowledged, so the indicator only flags searches that finish while
  // you're using the portal (and away from Matter Search — markSeen clears it).
  const recompute = useCallback((list: SearchJobSummary[]) => {
    const terminal = list.filter(isTerminal);
    if (!seededRef.current) {
      for (const s of terminal) ackRef.current.add(s.job_id);
      seededRef.current = true;
      setUnread(0);
      return;
    }
    setUnread(terminal.filter((s) => !ackRef.current.has(s.job_id)).length);
  }, []);

  const refresh = useCallback(async () => {
    if (mock || !token) return;
    try {
      const list = await listSearches(apiBase, token);
      setSearches(list);
      recompute(list);
    } catch {
      // transient — keep the last known list; the next poll retries
    }
  }, [apiBase, token, mock, recompute]);

  useEffect(() => {
    refresh();
    const id = window.setInterval(refresh, POLL_MS);
    return () => window.clearInterval(id);
  }, [refresh]);

  const markSeen = useCallback(() => {
    setSearches((list) => {
      for (const s of list) if (isTerminal(s)) ackRef.current.add(s.job_id);
      return list;
    });
    setUnread(0);
  }, []);

  const openSearch = useCallback((job: SearchJobSummary) => setOpenJob(job), []);
  const clearOpen = useCallback(() => setOpenJob(null), []);

  // Optimistic: drop the row immediately so the sidebar feels instant, then
  // confirm with the server; a failure re-fetches so the list stays truthful.
  const removeSearch = useCallback(
    async (jobId: string) => {
      setSearches((list) => list.filter((s) => s.job_id !== jobId));
      try {
        await deleteSearch(apiBase, jobId, token);
      } catch {
        refresh();
        throw new Error("Could not delete that search — please try again.");
      }
    },
    [apiBase, token, refresh],
  );

  const value = useMemo<SearchJobsValue>(
    () => ({ searches, refresh, openJob, openSearch, clearOpen, removeSearch, unread, markSeen }),
    [searches, refresh, openJob, openSearch, clearOpen, removeSearch, unread, markSeen],
  );

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useSearchJobs(): SearchJobsValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useSearchJobs must be used within a SearchJobsProvider");
  return v;
}

// Compact relative time ("just now", "5m ago", "3h ago", "2d ago").
export function timeAgo(epochSeconds?: number): string {
  if (!epochSeconds) return "";
  const secs = Math.max(0, Date.now() / 1000 - epochSeconds);
  if (secs < 60) return "just now";
  if (secs < 3600) return `${Math.floor(secs / 60)}m ago`;
  if (secs < 86400) return `${Math.floor(secs / 3600)}h ago`;
  return `${Math.floor(secs / 86400)}d ago`;
}
