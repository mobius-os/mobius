/* Waiting cards reuse the established bordered status panel; an action that needs the owner stays visible, others sit in the details. */
import { ChevronRight, Clock } from '@openai/apps-sdk-ui/components/Icon'

export default function WaitingCard({
  expanded, onToggle, ariaLabel, title, text, meta,
  rows = [], action, detailAction, stateLabel = 'Waiting',
}) {
  const label = stateLabel ? `${stateLabel}${text ? ` · ${text}` : ''}` : text
  return (
    <section className={`chat__wait-card${expanded ? ' chat__wait-card--expanded' : ''}`} aria-label={ariaLabel}>
      <button type="button" className="chat__wait-summary" title={title}
        aria-expanded={expanded} aria-label={`${expanded ? 'Collapse' : 'Expand'} ${ariaLabel}: ${label}${meta ? ` — ${meta}` : ''}`}
        onPointerDown={event => event.preventDefault()}
        onClick={onToggle}>
        <span className="chat__progress-identity" aria-hidden="true"><Clock width={14} height={14} /></span>
        <span className="chat__wait-text">{label}</span>
        {meta && <span className="chat__wait-meta">{meta}</span>}
        <ChevronRight className="chat__panel-chevron" width={14} height={14} aria-hidden="true" />
      </button>
      {action && <div className="chat__wait-action">
        <button type="button" className="chat__wait-cancel" onClick={action.onClick} disabled={action.disabled}>
          {action.label}
        </button>
        {action.error && <p className="chat__wait-error" role="alert">{action.error}</p>}
      </div>}
      {expanded && <div className="chat__wait-details">
        <dl className="chat__wait-detail-list">
          {rows.map(row => <div key={row.label} className={`chat__wait-detail-row${row.primary ? ' chat__wait-detail-row--primary' : ''}`}>
            <dt>{row.label}</dt><dd>{row.value}</dd>
          </div>)}
        </dl>
        {detailAction && <div className="chat__wait-detail-action">
          <button type="button" className="chat__wait-cancel" onClick={detailAction.onClick} disabled={detailAction.disabled}>
            {detailAction.label}
          </button>
          {detailAction.error && <p className="chat__wait-error" role="alert">{detailAction.error}</p>}
        </div>}
      </div>}
    </section>
  )
}
