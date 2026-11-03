import type { Answer } from "../types";
import { SECTION_ORDER } from "../types";
import { buildCitationIndex } from "../lib/citations";
import { answerClipboard } from "../lib/answerExport";
import { CopyButton } from "./CopyButton";
import { ReferenceList } from "./ReferenceList";
import { RichText } from "./RichText";

// Sections that carry a go/no-go signal get emphasis; the risk section is flagged.
const FLAG = new Set(["risks_blockers"]);
const GATE = new Set(["distribution_readiness"]);

export function StatusReport({
  matterName,
  matterType,
  answer,
}: {
  matterName: string;
  matterType: string | null;
  answer: Answer;
}) {
  const index = buildCitationIndex(answer);
  const hasContent = SECTION_ORDER.some(({ key }) => answer.sections[key]);
  // parse_ok === false means the model's JSON reply was cut off before it finished
  // (or didn't parse), leaving no sections. Never render that as a clean, blank
  // report — say what happened so staff retry instead of trusting an empty file.
  const generationFailed = answer.parse_ok === false || !hasContent;

  return (
    <article className="report" aria-label="Matter status file">
      <header className="report-head">
        <div>
          <div className="eyebrow">Status file</div>
          <h2 className="report-title">{matterName}</h2>
        </div>
        <div className="report-head-actions">
          {matterType && <span className={`type-tag type-${matterType}`}>{matterType}</span>}
          <CopyButton getContent={() => answerClipboard(matterName, matterType, answer)} />
        </div>
      </header>

      {generationFailed ? (
        <p className="banner banner-error">
          This summary couldn't be generated — the response ran long and was cut off before it
          finished. Please run the search again.
        </p>
      ) : (
        answer.insufficient_info && (
          <p className="banner banner-warn">
            Some sources were unavailable for this search, so parts of the picture may be missing
            — treat unconfirmed items as outstanding.
            {answer.insufficient_reasons?.[0] ? ` (${answer.insufficient_reasons[0]})` : ""}
          </p>
        )
      )}

      <dl className="sections">
        {SECTION_ORDER.map(({ key, title }) => {
          const text = answer.sections[key];
          const sources =
            key === "sources_reviewed" && Array.isArray(answer.sources_reviewed)
              ? answer.sources_reviewed
              : [];
          // "Sources reviewed" renders in its framework slot (11th, before the
          // suggested next action) and always shows the platform's deterministic
          // list under any model prose — it used to render after the last
          // section, reversing the framework's order.
          if (!text && sources.length === 0) return null;
          const cls = FLAG.has(key) ? "section flag" : GATE.has(key) ? "section gate" : "section";
          return (
            <div className={cls} key={key}>
              <dt>{title}</dt>
              <dd>
                {text && <RichText text={text} index={index} />}
                {sources.length > 0 && (
                  <ul className="sources-list">
                    {sources.map((s, i) => (
                      <li key={i}>{s}</li>
                    ))}
                  </ul>
                )}
              </dd>
            </div>
          );
        })}
      </dl>

      <footer className="report-foot">
        <ReferenceList ordered={index.ordered} />
      </footer>
    </article>
  );
}
