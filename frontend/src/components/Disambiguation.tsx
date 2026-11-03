import type { Candidate } from "../types";

const SYSTEM_LABEL: Record<string, string> = {
  estateably: "Estateably",
  clio: "Clio",
  quickbooks: "QuickBooks",
};

export function Disambiguation({
  candidates,
  onPick,
}: {
  candidates: Candidate[];
  onPick: (c: Candidate) => void;
}) {
  return (
    <section className="disambig" aria-label="Choose a matter">
      <p className="disambig-lead">More than one matter matches. Which one?</p>
      <ul className="candidates">
        {candidates.map((c, i) => (
          <li key={i}>
            <button className="candidate" onClick={() => onPick(c)}>
              <span className="candidate-name">{c.name}</span>
              <span className="candidate-meta">
                {c.matter_type && <span className={`type-tag type-${c.matter_type}`}>{c.matter_type}</span>}
                {c.systems.map((s) => (
                  <span className="sys-pill" key={s}>
                    {SYSTEM_LABEL[s] ?? s}
                  </span>
                ))}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
