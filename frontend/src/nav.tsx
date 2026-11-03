// The portal's modules. Single source of truth for the sidebar and the
// placeholder panels. `ready` marks the only module wired to the live pipeline
// today (Matter Search); the rest render a Placeholder until their phase lands.
//
// Icons are inline 24×24 SVG path strings (stroke-based, currentColor) so we
// pull in no icon dependency and they inherit the rail's ink colour.

import type { ReactNode } from "react";

export type TabId =
  | "matter-search"
  | "aml-export"
  | "quarterly-reports"
  | "activity-log"
  | "sanction-screener"
  | "integrations";

export interface ModuleTab {
  id: TabId;
  label: string;
  /** One line shown under the title on the placeholder panel. */
  blurb: string;
  /** Status line for the placeholder panel (empty for the ready module). */
  status: string;
  /** True only for the module backed by a live pipeline. */
  ready: boolean;
  /** SVG inner markup (paths) drawn inside a 24×24 viewBox. */
  icon: ReactNode;
}

// Stroke icons: 24×24 viewBox, no fill, currentColor stroke (see .nav-icon svg).
const icon = (paths: ReactNode): ReactNode => paths;

export const TABS: ModuleTab[] = [
  {
    id: "matter-search",
    label: "Matter Search",
    blurb: "Ask about any matter and get a source-cited status file.",
    status: "",
    ready: true,
    icon: icon([
      <circle key="a" cx="11" cy="11" r="7" />,
      <line key="b" x1="16.5" y1="16.5" x2="21" y2="21" />,
    ]),
  },
  {
    id: "aml-export",
    label: "AML Export",
    blurb:
      "Monthly deduplicated active-client list from Clio and Estateably, formatted for the sanctions screener.",
    status: "",
    ready: true,
    icon: icon([
      <path key="a" d="M6 3h9l5 5v13a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1z" />,
      <path key="b" d="M14 3v5h5" />,
      <path key="c" d="M9 13h6M9 16h6" />,
    ]),
  },
  {
    id: "quarterly-reports",
    label: "Quarterly Reports",
    blurb:
      "Per-matter quarterly trust statement drafts (cover letter + trust ledger) for human review and sending.",
    status: "",
    ready: true,
    icon: icon([
      <path key="a" d="M4 20V10M9.5 20V4M15 20v-7M20.5 20V8" />,
      <line key="b" x1="3" y1="20.5" x2="21.5" y2="20.5" />,
    ]),
  },
  {
    id: "activity-log",
    label: "Activity Logs",
    blurb:
      "Who queried what, when, and which sources were read — the chain-of-custody audit trail for every request.",
    status: "",
    ready: true,
    icon: icon([
      <circle key="a" cx="12" cy="12" r="8" />,
      <path key="b" d="M12 7v5l3.5 2" />,
    ]),
  },
  {
    id: "sanction-screener",
    label: "Sanction Screener",
    blurb:
      "Screen a client name list against the UN, SEMA, and Public Safety Canada watchlists, adjudicate matches, and issue a Certificate of Search.",
    status: "",
    ready: true,
    icon: icon([
      <path key="a" d="M12 3l7 3v5c0 4.5-3 8.2-7 9.5C8 19.2 5 15.5 5 11V6l7-3z" />,
      <path key="b" d="M9 11.5l2 2 4-4.5" />,
    ]),
  },
  {
    id: "integrations",
    label: "Integrations",
    blurb:
      "Live health of every connected system — Clio, Estateably, Outlook, and QuickBooks — with a downtime log and a suggested fix when one is down.",
    status: "",
    ready: true,
    // A heartbeat / pulse line — status monitoring.
    icon: icon([<path key="a" d="M3 12h4l2.5 6 4-13 2.5 7H21" />]),
  },
];

export const DEFAULT_TAB: TabId = "matter-search";
