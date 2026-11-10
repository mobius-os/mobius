/* WaitHistoryCard preserves settled monitor and dependency outcomes beside chat history. */

import { ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
import DependencyLink from './DependencyLink.jsx'
import DependencyFullText from './DependencyFullText.jsx'
import LifecycleIcon, { LifecycleOutcome } from './LifecycleIcon.jsx'
import { waitHistoryViewModel } from './waitHistory.js'

export default function WaitHistoryCard({ summary, onInternalNav }) {
  const view = waitHistoryViewModel(summary)
  if (!view) return null

  if (summary.target_kind) {
    const header = <>
      <LifecycleIcon kind="wait" />
      <span className="chat__goal-history-copy">
        <span className="chat__goal-history-kicker">{view.kicker}</span>
        <strong className="chat__goal-history-objective">{view.condition}</strong>
        {view.metadata && <span className="chat__goal-history-meta">{view.metadata}</span>}
      </span>
    </>
    const hasContext = !summary.source_deleted && !!summary.source_chat_title
    // A claim text shown in part reads whole here, as in the Goal plan.
    const fullText = summary.truncated && summary.id && <DependencyFullText dependencyId={summary.id} />
    return <aside className="chat__goal-history chat__wait-history" aria-label={view.ariaLabel}>
      {hasContext ? <details className="chat__goal-history-details">
        <summary className="chat__goal-history-summary">
          {header}
          <ChevronRight className="chat__panel-chevron" width={14} height={14} aria-hidden="true" />
        </summary>
        <div className="chat__dependency-history-detail">
          <DependencyLink dependency={summary} onInternalNav={onInternalNav} />
          {fullText}
        </div>
      </details> : <>{header}{fullText}</>}
    </aside>
  }

  return (
    <aside
      className={`chat__goal-history chat__wait-history chat__wait-history--${view.tone}`}
      aria-label={view.ariaLabel}
    >
      <LifecycleIcon kind="wait" />
      <div className="chat__goal-history-copy">
        <span className="chat__goal-history-kicker chat__wait-history-kicker">
          <LifecycleOutcome tone={view.tone} />{view.kicker}
        </span>
        <strong className="chat__goal-history-objective">{view.condition}</strong>
        {view.metadata && (
          <span className="chat__goal-history-meta">{view.metadata}</span>
        )}
      </div>
    </aside>
  )
}
