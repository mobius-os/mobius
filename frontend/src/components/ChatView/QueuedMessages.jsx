import { useState } from 'react'
import { DoubleChevronRight } from '@openai/apps-sdk-ui/components/Icon'
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
 * Visual model: a soft, slightly raised stack — distinct from the chat
 * transcript so it's clear these are "not yet sent" turns. Lives between
 * the chat list and the input form. Empty queue → nothing rendered.
 */
export default function QueuedMessages({
  items, onCancel, onSteerOne, steerActive, steerBusy = false,
}) {
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
        // Queued tray lives inside the composer footer. Keep textarea focus
        // on touch/mouse taps so expanding/collapsing the tray does not
        // collapse the soft keyboard mid-composition.
        onPointerDown={(e) => e.preventDefault()}
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
                {steerActive && msg.serverTs === true && (
                  // Per-row fast-forward (owner ask, 2026-07-17): the same
                  // double-chevron as the composer's steer button, icon-only —
                  // send exactly THIS message into the running turn now.
                  // Rendered only while a turn is live and the row is
                  // server-confirmed (an optimistic row's cid selects nothing
                  // on the backend).
                  <button
                    type="button"
                    className="queued__steer"
                    onPointerDown={(e) => e.preventDefault()}
                    onClick={() => onSteerOne?.(cidOf(msg))}
                    aria-label="Send this queued message now"
                    title="Send now"
                    disabled={steerBusy}
                  >
                    <DoubleChevronRight width={14} height={14} />
                  </button>
                )}
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
