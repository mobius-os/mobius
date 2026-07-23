import { useRef, useState } from 'react'
import { preserveTogglePosition } from './preserveTogglePosition.js'

// Shared shell for "marker" messages — system/product moments that are neither
// agent prose nor a tool call. A compaction summary is the only marker today;
// interrupted-turn and other system notes can adopt this shell later so every
// marker reads as one family (one card shape, one disclosure motion) instead of
// each inventing its own look or masquerading as a chat bubble.
//
// Extracted from CompactionCard with no visual change — the default look IS the
// accent-tinted summary divider that card established (`.chat__marker*` CSS,
// renamed 1:1 from `.chat__compaction*`).
//
// `children` is the optional collapsible body. With children the header is a
// real toggle button; with none the card is a static labeled divider (no
// chevron, non-interactive header).
export default function MarkerCard({ icon, title, subtitle, children }) {
  const [open, setOpen] = useState(false)
  const headerRef = useRef(null)
  const collapsible = !!children
  const cls = 'chat__marker' + (open ? ' chat__marker--open' : '')

  const label = (
    <>
      <span className="chat__marker-icon" aria-hidden="true">{icon}</span>
      <span className="chat__marker-label">
        <span className="chat__marker-title">{title}</span>
        {subtitle && <span className="chat__marker-sub">{subtitle}</span>}
      </span>
      {collapsible && (
        <span className="chat__marker-toggle" aria-hidden="true">
          <svg
            className={`chat__chevron${open ? '' : ' chat__chevron--collapsed'}`}
            width="10" height="10" viewBox="0 0 10 10" fill="none"
          >
            <path d="M2 4l3 3 3-3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </span>
      )}
    </>
  )

  return (
    <div className={cls}>
      {collapsible ? (
        <button
          ref={headerRef}
          type="button"
          className="chat__marker-header"
          // Toggling the compaction body changes height above the scroll anchor;
          // preserve the reader's position first, like every other disclosure.
          onClick={() => {
            preserveTogglePosition(headerRef.current)
            setOpen(o => !o)
          }}
          aria-expanded={open}
        >
          {label}
        </button>
      ) : (
        <div className="chat__marker-header chat__marker-header--static">
          {label}
        </div>
      )}
      {collapsible && open && (
        <div className="chat__marker-body">{children}</div>
      )}
    </div>
  )
}
