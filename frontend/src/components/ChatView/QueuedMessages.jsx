import { useState } from 'react'

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
 * Visual model: a soft, slightly raised stack — distinct from the chat
 * transcript so it's clear these are "not yet sent" turns. Lives between
 * the chat list and the input form. Empty queue → nothing rendered.
 */
export default function QueuedMessages({ items, onCancel }) {
  const [expanded, setExpanded] = useState(() => new Set())
  const [collapsed, setCollapsed] = useState(false)

  if (!items || items.length === 0) return null

  // Stable key: prefer `cid` (assigned by ChatView either fresh-uuid
  // for optimistic entries or `s-<ts>` for server-hydrated). Fall back
  // to ts for any legacy entry that lacks one. Using ts alone breaks
  // when the optimistic ts is replaced by the server ts — React
  // re-mounts the row under the new key and loses the expanded state.
  function keyOf(msg) {
    return msg.cid ?? `t-${msg.ts}`
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

  function onHdrKeyDown(e) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      toggleCollapsed()
    }
  }

  const itemsId = 'queued-items'

  return (
    <div
      className={`queued${collapsed ? ' queued--collapsed' : ''}`}
      role="list"
      aria-label="Queued messages"
    >
      <div
        className="queued__hdr"
        role="button"
        tabIndex={0}
        onClick={toggleCollapsed}
        onKeyDown={onHdrKeyDown}
        aria-expanded={!collapsed}
        aria-controls={itemsId}
      >
        <span className="queued__count">
          {items.length} queued
        </span>
        <span className="queued__hint">
          Will send after the current turn finishes
        </span>
        <svg
          className={`queued__hdr-chevron${collapsed ? ' queued__hdr-chevron--collapsed' : ''}`}
          width="10" height="10" viewBox="0 0 10 10" fill="none"
          aria-hidden="true"
        >
          <path d="M2 4l3 3 3-3" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </div>
      {!collapsed && (
        <div id={itemsId} className="queued__items">
          {items.map(msg => {
            const key = keyOf(msg)
            const text = msg.content || ''
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
                  onClick={() => onCancel?.(msg.ts)}
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
