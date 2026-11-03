import type { QuickAnswer as QuickAnswerData } from "../types";
import { buildCitationIndexFromTexts } from "../lib/citations";
import { quickClipboard } from "../lib/answerExport";
import { CopyButton } from "./CopyButton";
import { ReferenceList } from "./ReferenceList";
import { RichText } from "./RichText";

// A Tier-0 quick answer: a concise, source-cited reply to a narrow question,
// with an escalation to the full status file. No model was involved.
export function QuickAnswer({
  matterName,
  matterType,
  quick,
  onViewFull,
}: {
  matterName: string;
  matterType: string | null;
  quick: QuickAnswerData;
  onViewFull: () => void;
}) {
  const index = buildCitationIndexFromTexts([quick.text], quick.citations);

  return (
    <article className="report quick" aria-label="Quick answer">
      <header className="report-head">
        <div>
          <div className="eyebrow">Quick answer</div>
          <h2 className="report-title">{matterName}</h2>
        </div>
        <div className="report-head-actions">
          {matterType && <span className={`type-tag type-${matterType}`}>{matterType}</span>}
          <CopyButton getContent={() => quickClipboard(matterName, matterType, quick)} />
        </div>
      </header>

      <div className="quick-body">
        <RichText text={quick.text} index={index} />
      </div>

      <ReferenceList ordered={index.ordered} />

      <div className="quick-foot">
        <button className="link-btn ghost" onClick={onViewFull}>
          View full status file
        </button>
      </div>
    </article>
  );
}
