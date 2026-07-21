import { forwardRef } from 'react'


// Every activity line uses the same fixed-width glyph lane. Tool kinds select
// their matching glyph; reasoning has its own mark so a thinking-only stretch
// remains visibly part of the activity system instead of becoming bare text.
export function ActivityTypeIcon({ kind }) {
  const common = {
    viewBox: '0 0 16 16', width: 13, height: 13, fill: 'none',
    stroke: 'currentColor', strokeWidth: 1.5,
    strokeLinecap: 'round', strokeLinejoin: 'round',
  }
  if (kind === 'reasoning') {
    return (
      <svg {...common}>
        <path d="M8 1.5c.5 3.3 2.2 5 5.5 5.5C10.2 7.5 8.5 9.2 8 12.5 7.5 9.2 5.8 7.5 2.5 7 5.8 6.5 7.5 4.8 8 1.5Z" />
      </svg>
    )
  }
  if (kind === 'terminal') {
    return (
      <svg {...common}>
        <rect x="1.5" y="3" width="13" height="10" rx="2" />
        <path d="M4.5 6.5 7 8.5l-2.5 2" /><path d="M8.5 10.5h3" />
      </svg>
    )
  }
  if (kind === 'files') {
    return (
      <svg {...common}>
        <path d="M4 1.5h5l3 3v10H4z" /><path d="M9 1.5v3h3" />
      </svg>
    )
  }
  if (kind === 'search') {
    return (
      <svg {...common}>
        <circle cx="7" cy="7" r="4.5" /><path d="M10.5 10.5 14 14" />
      </svg>
    )
  }
  if (kind === 'edit') {
    return (
      <svg {...common}>
        <path d="m11.3 2.2 2.5 2.5L6 12.5l-3.2.7.7-3.2z" />
      </svg>
    )
  }
  if (kind === 'web') {
    return (
      <svg {...common}>
        <circle cx="8" cy="8" r="5.5" /><path d="M2.5 8h11" />
        <path d="M8 2.5c2 1.8 2 9.2 0 11-2-1.8-2-9.2 0-11z" />
      </svg>
    )
  }
  if (kind === 'plan') {
    return (
      <svg {...common}>
        <path d="M3 4.5h10" /><path d="M3 8h10" /><path d="M3 11.5h6" />
      </svg>
    )
  }
  if (kind === 'image') {
    return (
      <svg {...common}>
        <rect x="2" y="3" width="12" height="10" rx="2" />
        <circle cx="6" cy="6.5" r="1.1" />
        <path d="M2.5 11.5 6 8.5l2.5 2 2-1.5 2.5 2.5" />
      </svg>
    )
  }
  return (
    <svg {...common}>
      <circle cx="8" cy="8" r="2.2" fill="currentColor" stroke="none" />
    </svg>
  )
}

const ActivityLineHeader = forwardRef(function ActivityLineHeader({
  text,
  displayState,
  iconKind,
  exitCode = null,
  interactive = false,
  open = false,
  ariaLabel,
  controlsId,
  onToggle,
  count = null,
}, ref) {
  const Header = interactive ? 'button' : 'div'

  return (
    <Header
      ref={ref}
      type={interactive ? 'button' : undefined}
      className={
        `chat__activity-header${interactive ? '' : ' chat__activity-header--static'}`
      }
      onClick={interactive ? onToggle : undefined}
      aria-expanded={interactive ? open : undefined}
      aria-controls={interactive ? controlsId : undefined}
      aria-label={ariaLabel}
      role={interactive ? undefined : 'status'}
    >
      <span
        className="chat__activity-icon"
        data-activity-kind={displayState === 'error' ? undefined : iconKind}
        aria-hidden="true"
      >
        {displayState === 'error' ? (
          <svg viewBox="0 0 16 16" width="13" height="13" fill="none"
            stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"
            strokeLinejoin="round">
            <path d="M8 2 15 14H1z" /><path d="M8 6v4" /><path d="M8 12h.01" />
          </svg>
        ) : (
          <ActivityTypeIcon kind={iconKind} />
        )}
      </span>
      <span className="chat__activity-label">
        <span className="chat__activity-label-text">{text}</span>
        {displayState === 'running' && (
          <span className="chat__activity-label-sweep" aria-hidden="true">{text}</span>
        )}
      </span>
      {count && (
        // A delegating turn's helper rollup ("2 running · 1 done") — the header
        // owns it so it reads at a glance without expanding the line.
        <span className="chat__activity-count">{count}</span>
      )}
      {displayState === 'error' && exitCode != null && (
        <span className="chat__activity-chip">exit {exitCode}</span>
      )}
    </Header>
  )
})

export default ActivityLineHeader
