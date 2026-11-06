import { useRef, useState } from 'react'
import {
  ArrowRotateCw,
  ChevronDown,
  DoubleChevronRight,
  Paperclip,
  Pencil,
  X,
} from '@openai/apps-sdk-ui/components/Icon'
import { stripAugmentation } from './msgText.js'
import { cidOf } from './messageIdentity.js'
import { queuedHint } from './queuedHint.js'
import { queuedFilesLabel, queuedPreview } from './queuedPreview.js'
import {
  pointerSelectionChangedWithin,
  textSelectionSnapshot,
} from '../../lib/selectableTextControl.js'

/**
 * Queued-messages tray rendered above the chat input.
 *
 * The header is tappable to collapse/expand the list of queued items.
 * When collapsed, the header still shows (so the user knows there are
 * pending messages) but the items below are hidden. Expanded by default.
 *
 * Each queued message is itself a collapsible row showing a truncated
 * first line. Click the row to expand and see the full content. The pencil
 * opens the message in the chat box (ChatInputBar's history), where Enter
 * saves it in the same queue position; while it is open there the row says
 * so instead of offering the pencil. The X cancels (DELETE the pending
 * message on the backend).
 *
 * Visual model: a soft, slightly raised stack — distinct from the chat
 * transcript so it's clear these are "not yet sent" turns. Lives between
 * the chat list and the input form. Empty queue → nothing rendered.
 */
export default function QueuedMessages({
  items, onCancel, onEditInComposer, inComposerCid = null, onRetry, onSteerOne,
  steerActive, steerBusy = false, turnActive = false, online = true,
  restarting = false,
}) {
  const [expanded, setExpanded] = useState(() => new Set())
  const [collapsed, setCollapsed] = useState(false)
  const pointerSelectionRef = useRef(null)

  if (!items || items.length === 0) return null

  // The caption states the real reason these are waiting (see queuedHint).
  const rejectedCount = items.filter(msg => msg.recoveryAvailable).length
  const hint = rejectedCount ? `${rejectedCount} ${rejectedCount === 1 ? 'needs' : 'need'} attention — kept here` : queuedHint({ turnActive, online, restarting })

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
          {hint}
        </span>
        <ChevronDown
          className={`queued__hdr-chevron${collapsed ? ' queued__hdr-chevron--collapsed' : ''}`}
          width={10}
          height={10}
          aria-hidden="true"
        />
      </div>
      {!collapsed && (
        <div id={itemsId} className="queued__items">
          {items.map(msg => {
            const key = keyOf(msg)
            const text = stripAugmentation(msg.content || '')
            const isExpanded = expanded.has(key)
            const { preview, needsTruncation } = queuedPreview(text)
            const filesLabel = queuedFilesLabel(msg.attachments)
            const filesOnly = filesLabel && !text
            const MessageSurface = needsTruncation ? 'button' : 'div'
            const inComposer = inComposerCid === key
            const rejected = msg.recoveryAvailable

            return (
              <div
                key={key}
                className={`queued__row${isExpanded ? ' queued__row--expanded' : ''}${inComposer ? ' queued__row--in-composer' : ''}`}
                role="listitem"
              >
                <MessageSurface
                  type={needsTruncation ? 'button' : undefined}
                  className={`queued__toggle${needsTruncation ? '' : ' queued__toggle--static'}`}
                  onPointerDown={needsTruncation ? () => {
                    pointerSelectionRef.current = textSelectionSnapshot()
                  } : undefined}
                  onClick={needsTruncation ? (event) => {
                    const selectionBeforePointer = pointerSelectionRef.current
                    pointerSelectionRef.current = null
                    if (
                      event.detail !== 0
                      && pointerSelectionChangedWithin(
                        selectionBeforePointer,
                        event.currentTarget,
                      )
                    ) return
                    toggle(key)
                  } : undefined}
                  aria-expanded={needsTruncation ? isExpanded : undefined}
                  aria-label={needsTruncation
                    ? (isExpanded ? 'Collapse message' : 'Expand message')
                    : undefined}
                >
                  {needsTruncation && (
                    <ChevronDown
                      className={`queued__chevron${isExpanded ? ' queued__chevron--open' : ''}`}
                      width={10}
                      height={10}
                      aria-hidden="true"
                    />
                  )}
                  {(!filesOnly || rejected) && (
                    <span className="queued__text">
                      {isExpanded ? text : preview}
                      {rejected && <span className="queued__rejection" role="status">
                        Send rejected. Your message is kept here.
                      </span>}
                    </span>
                  )}
                  {filesLabel && (
                    <span className={`queued__files${filesOnly ? ' queued__files--only' : ''}`}>
                      <Paperclip width={14} height={14} aria-hidden="true" />
                      <span className="queued__files-name">{filesLabel}</span>
                    </span>
                  )}
                </MessageSurface>
                {rejected && (
                  <button
                    type="button"
                    className="queued__action"
                    onPointerDown={event => event.preventDefault()}
                    onClick={() => onRetry?.(cidOf(msg))}
                    aria-label="Retry this message"
                    title="Retry this message"
                  >
                    <ArrowRotateCw width={16} height={16} aria-hidden="true" />
                  </button>
                )}
                {steerActive && !rejected && !inComposer && (
                  // Per-row fast-forward (owner ask, 2026-07-17): the same
                  // double-chevron as the composer's steer button, in the
                  // queue action's compact neutral well — send exactly THIS
                  // message into the running turn now. Order is send · edit ·
                  // cancel (owner ask, 2026-08-21).
                  // Render it with the optimistic row so the action well and
                  // cancel-X arrive together. An early tap waits for this
                  // row's queue write in ChatView before force-steering it.
                  // The row open in the chat box has none: it would send the
                  // saved text and ignore the edit there.
                  <button
                    type="button"
                    className="queued__action queued__steer"
                    onPointerDown={(e) => e.preventDefault()}
                    onTouchEnd={(e) => {
                      e.preventDefault()
                      onSteerOne?.(cidOf(msg))
                    }}
                    onClick={() => onSteerOne?.(cidOf(msg))}
                    aria-label="Send this queued message now"
                    title="Send now"
                    disabled={steerBusy}
                  >
                    <DoubleChevronRight width={16} height={16} aria-hidden="true" />
                  </button>
                )}
                {inComposer ? (
                  <span className="queued__in-composer">In the chat box</span>
                ) : (
                  <button
                    type="button"
                    className="queued__action queued__edit"
                    // Keep the composer focused; the chat box focuses itself
                    // inside this tap so iOS keeps the soft keyboard.
                    onPointerDown={(e) => e.preventDefault()}
                    onClick={() => onEditInComposer?.(cidOf(msg))}
                    aria-label="Edit queued message"
                    title={rejected ? 'Retry keeps the original message unchanged.' : 'Edit in the chat box'}
                    disabled={rejected}
                  >
                    <Pencil width={16} height={16} aria-hidden="true" />
                  </button>
                )}
                <button
                  type="button"
                  className="queued__action queued__cancel"
                  onPointerDown={(e) => e.preventDefault()}
                  onClick={() => onCancel?.(cidOf(msg))}
                  aria-label={rejected ? 'Discard local copy of this message' : 'Cancel queued message'}
                  title={rejected ? 'Discard local copy' : 'Cancel'}
                >
                  <X width={16} height={16} aria-hidden="true" />
                </button>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
