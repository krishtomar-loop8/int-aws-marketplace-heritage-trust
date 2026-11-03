import type { NumberedCite } from "../lib/citations";
import { SYSTEM_LABEL } from "./Citation";

// The numbered references at the foot of a report/quick answer. Each Outlook citation
// carries a masked body excerpt (the `[N]` markers link here in-app), revealed by a native
// "Show email" disclosure so a reader can verify a cited email without opening Outlook —
// the source label still links out (deep-link) for anyone who has access to that mailbox.
export function ReferenceList({ ordered }: { ordered: NumberedCite[] }) {
  if (ordered.length === 0) return null;
  return (
    <div className="references">
      <span className="foot-label">References</span>
      <ul>
        {ordered.map((c) => {
          const label = SYSTEM_LABEL[c.system] ?? c.system;
          return (
            <li id={`ref-${c.n}`} key={c.n}>
              <span className="ref-num">[{c.n}]</span>{" "}
              {c.url ? (
                <a href={c.url} target="_blank" rel="noopener noreferrer">
                  {label}
                </a>
              ) : (
                label
              )}
              {c.note ? ` — ${c.note}` : ""}
              {c.excerpt && (
                <details className="ref-evidence">
                  <summary>Show email</summary>
                  <div className="ref-excerpt">
                    {c.date && <span className="ref-date">{c.date.slice(0, 10)}</span>}
                    {c.excerpt}
                  </div>
                </details>
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
