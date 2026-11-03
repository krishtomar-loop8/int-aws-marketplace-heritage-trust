import type { ModuleTab } from "../nav";

// Shown for every module that isn't wired to a live pipeline yet. Keeps the
// portal navigable and sets expectations without pretending the feature exists.
export function Placeholder({ tab }: { tab: ModuleTab }) {
  return (
    <div className="placeholder">
      <div className="placeholder-card">
        <span className="placeholder-icon" aria-hidden="true">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth={1.5}
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            {tab.icon}
          </svg>
        </span>
        <h1 className="placeholder-title">{tab.label}</h1>
        <p className="placeholder-blurb">{tab.blurb}</p>
        {tab.status && <p className="placeholder-status">{tab.status}</p>}
      </div>
    </div>
  );
}
