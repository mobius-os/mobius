import { useState } from 'react'
import { stripAugmentation } from './msgText.js'
import { cidOf } from './chatRuntimeState.js'

const TRUNCATE_AT = 80

/**
 * Queued-messages tray rendered above the chat input.
 *
 * The header is tappable to collapse/expand the list of queued items.
 * When collapsed, the header still shows (so the user knows there are
 * pending messages) but the items below are hidden. Expanded by default.
 *
 * Each queued message is itself a collapsible row showing a truncated
 * first line. Click the row to expand and see the full content. Click
 * the X to cancel (DELETE the pending message on the backend).
 *
 * Visual model: an EXTENSION OF THE TRANSCRIPT (owner ask, 2026-07-17) —
 * right-aligned pending user bubbles leading the foot stack, directly under
 * the chat, with a quiet one-line caption as the collapse control. No card
 * tray around them: they read as upcoming turns, not system chrome. Opaque
 * low-accent fill + hairline (never dashed borders or whole-row opacity —
 * those read as drop zones / disabled and cost contrast). Empty queue →
 * nothing rendered.
 */
export default function QueuedMessages({ items, onCancel }) {
  const [expanded, setExpanded] = useState(() => new Set())
  const [collapsed, setCollapsed] = useState(false)

  if (!items || items.length === 0) return null

  // Stable key: the row's `cid` (client-minted, or a `legacy-<ts>`
  // derivation for pre-cid rows). cid is minted once at compose time and
  // never changes across the optimistic→confirm ts update, so the row keeps
  // its expanded state instead of remounting under a new key.
  function keyOf(msg) {
    return cidOf(msg)
  }

  function toggle(key) {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  function toggleCollapsed() {
    setCollapsed(c => !c)
  }

  const itemsId = 'queued-items'

  return (
    <div
      className={`queued${collapsed ? ' queued--collapsed' : ''}`}
      aria-label="Queued messages"
    >
      <button
        type="button"
        className="queued__hdr"
        // Queued bubbles lead the composer footer. Keep textarea focus on
        // touch/mouse taps so expanding/collapsing does not collapse the
        // soft keyboard mid-composition. A real <button> (not role=button)
        // so the caption is natively focusable/activatable — the root is a
        // neutral labelled section, list semantics live on the items wrap.
        onPointerDown={(e) => e.preventDefault()}
        onClick={toggleCollapsed}
        aria-expanded={!collapsed}
        aria-controls={itemsId}
      >
        <span className="queued__count">
          {items.length} queued · Send after this turn
        </span>
        <svg
          className={`queued__hdr-chevron${collapsed ? ' queued__hdr-chevron--collapsed' : ''}`}
          width="10" height="10" viewBox="0 0 10 10" fill="none"
          aria-hidden="true"
        >
          <path d="M2 4l3 3 3-3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </button>
      {!collapsed && (
        <div id={itemsId} className="queued__items" role="list">
          {items.map(msg => {
            const key = keyOf(msg)
            const text = stripAugmentation(msg.content || '')
            const isExpanded = expanded.has(key)
            const needsTruncation = text.length > TRUNCATE_AT || text.includes('\n')
            const firstLine = text.split('\n')[0]
            const preview = firstLine.length > TRUNCATE_AT
              ? firstLine.slice(0, TRUNCATE_AT) + '…'
              : firstLine + (text.includes('\n') ? ' …' : '')

            return (
              <div
                key={key}
                className={`queued__row${isExpanded ? ' queued__row--expanded' : ''}`}
                role="listitem"
              >
                <button
                  type="button"
                  className="queued__toggle"
                  onPointerDown={(e) => e.preventDefault()}
                  onClick={() => needsTruncation && toggle(key)}
                  aria-expanded={isExpanded}
                  aria-label={isExpanded ? 'Collapse message' : 'Expand message'}
                  disabled={!needsTruncation}
                >
                  {needsTruncation && (
                    <svg
                      className={`queued__chevron${isExpanded ? ' queued__chevron--open' : ''}`}
                      width="10" height="10" viewBox="0 0 10 10" fill="none"
                      aria-hidden="true"
                    >
                      <path d="M2 4l3 3 3-3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
                    </svg>
                  )}
                  <span className="queued__text">
                    {isExpanded ? text : preview}
                  </span>
                </button>
                <button
                  type="button"
                  className="queued__cancel"
                  onPointerDown={(e) => e.preventDefault()}
                  onClick={() => onCancel?.(cidOf(msg))}
                  aria-label="Cancel queued message"
                  title="Cancel"
                >
                  <svg width="11" height="11" viewBox="0 0 11 11" fill="none" aria-hidden="true">
                    <path d="M2 2l7 7M9 2l-7 7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"/>
                  </svg>
                </button>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
