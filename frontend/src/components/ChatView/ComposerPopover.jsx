/**
 * ComposerPopover — a trigger button in the chat composer and the popover
 * it opens. Two independent instances share this component (see ChatView's
 * `leftButtons`): the `+` button (attach files + chat summary/context) and
 * the brain-shaped model-usage gauge (the model/effort picker only). Each
 * instance owns its own open/close state, trigger icon, and section
 * visibility — they are not the same popover. Splitting `showModel` from
 * `showAttachAndContext` keeps the positioning/focus machinery below
 * (shared, load-bearing) single-sourced while letting the two triggers open
 * genuinely different content, per the product decision that model choice
 * belongs to the usage-gauge icon, not `+`.
 *
 *   1. Attach files  — calls `onAttachClick` (parent owns the hidden
 *      <input type="file"> so it can clear .value after each pick).
 *      Shown when `showAttachAndContext`.
 *   2. Model / effort / automation — renders <ChatSettingsPanel> when a
 *      chatInfo is available; omitted on a fresh empty chat where chatInfo
 *      hasn't loaded yet. Shown when `showModel`.
 *   3. Chat summary / agent context — opens the two owner-facing continuity
 *      viewers. Shown when `showAttachAndContext`.
 *
 * Open/close state, outside-click, and Escape live here. The trigger
 * is positioned as a sibling of the pill in `.chat__form`. The popover
 * is absolutely positioned relative to `.composer-plus` (the wrapper
 * around the trigger button), which has `position: relative`. Don't
 * remove that `position: relative` thinking `.chat__form` is the
 * anchor — the form is only relative so other absolutely-positioned
 * children (none today) could anchor to it.
 *
 * A draft-first New Chat uses the `+` instance of this component with
 * `pending`. That renders the canonical trigger in its final geometry, but
 * keeps it disabled and omits dialog semantics until the server-backed chat
 * is ready. Keeping the pending state here prevents the provisional
 * composer from maintaining a second lookalike button that can drift from
 * the real control.
 *
 * Soft-keyboard contract: opening or using this popover preserves whether the
 * owning textarea was focused. The trigger suppresses native button focus
 * and records that state synchronously. The popover has one bubbling
 * pointer boundary that suppresses descendant focus and restores the textarea
 * on the next frame only when it was focused before opening. That next-frame
 * repair covers mobile Safari dropping focus during the later click without
 * stealing keyboard focus from keyboard-operated controls. ChatInputBar owns
 * the separate native file-picker return. Opening with the keyboard down keeps
 * the defensive next-frame blur for Android's occasional focus restoration.
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { FileDocument, InfoCircle, Paperclip, Plus } from '@openai/apps-sdk-ui/components/Icon'
import ChatSettingsPanel from './ChatSettingsPanel.jsx'
import { popoverMaxHeight, nearestClipTop } from './composerPopoverHeight.js'
import { focusComposerElement } from './composerFocusPolicy.js'
import {
  captureLayoutSpace,
  clientLengthToLayout,
  clientPointToLayout,
} from '../../lib/layoutSpace.js'

export default function ComposerPopover({
  chatInfo,
  chatId,
  onAttachClick,
  onChangeChatInfo,
  // Live-derived in the parent: `chatInfo.has_assistant_turns` is
  // set once on mount and never refreshed when the running turn
  // finishes. Without the live override, cross-provider picks would
  // skip the confirmation + handoff after the first reply lands in
  // the same session. Parent ORs the persisted
  // flag with a `messages.some(m => m.role === 'assistant')`
  // check and passes the result down.
  hasAssistantTurns,
  autoResumeEnabled,
  autoResumeSaving,
  autoResumeError,
  onAutoResumeChange,
  restartResumeEnabled,
  restartResumeSaving,
  restartResumeError,
  onRestartResumeChange,
  providerSwitchState,
  settingsSaveTailRef,
  composerInputRef,
  onOpenInspector,
  onOpenSummary,
  embedded = false,
  pending = false,
  // Which sections this instance renders — see the module docstring.
  showAttachAndContext = true,
  showModel = true,
  // Trigger presentation, overridable so a second instance (the usage
  // gauge) doesn't have to render `+`.
  triggerIcon = <Plus width={26} height={26} />,
  triggerClassName = '',
  triggerAriaLabel = 'Attach files',
  dialogAriaLabel = 'Chat options',
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef(null)
  const triggerRef = useRef(null)
  // Tracks whether the chat textarea was focused at the moment the
  // popover opened. If yes, refocus after a picker action so the
  // soft keyboard stays open. If no (user tapped + with keyboard
  // down), don't refocus — popping the keyboard up unexpectedly is
  // worse than the textarea losing focus.
  //
  // Captured SYNCHRONOUSLY inside the `+` button's onClick so we
  // read activeElement at the exact moment of the tap. A previous
  // version captured this in a useEffect on `[open]`, which fires
  // AFTER React commits — on iOS Safari the focus state can shift
  // between the click handler and the post-commit effect, leaving
  // the ref stale. Sync capture in onClick is reliable.
  const wasInputFocusedRef = useRef(false)
  // Measured cap on the panel's height: the space above the trigger inside both
  // the chat pane (which clips with `overflow: hidden`) and the keyboard-shrunk
  // visible viewport. See composerPopoverHeight.js for why CSS viewport units
  // can't express either boundary.
  const [maxHeight, setMaxHeight] = useState(null)

  function preservePickerInputFocus(event) {
    event.preventDefault()
    if (!wasInputFocusedRef.current) return
    requestAnimationFrame(() => focusComposerElement(composerInputRef?.current))
  }

  useLayoutEffect(() => {
    if (!open) return
    const measure = () => {
      const trigger = triggerRef.current
      if (!trigger) return
      const rect = trigger.getBoundingClientRect()
      const rootSpace = captureLayoutSpace(document.documentElement)
      const pointY = y => clientPointToLayout({ x: 0, y }, rootSpace).y
      const deltaY = y => clientLengthToLayout(y, rootSpace)
      const triggerTop = pointY(rect.top)
      const triggerBottom = pointY(rect.bottom)
      const viewportTop = deltaY(window.visualViewport?.offsetTop || 0)
      const viewportHeight = deltaY(window.visualViewport?.height || 0)
      const clipTop = pointY(nearestClipTop(trigger))
      setMaxHeight(popoverMaxHeight({
        triggerTop,
        // `triggerBottom` + `viewportHeight` are not extra precision — they are
        // how the helper tells which coordinate space `rect` is in. iOS reports
        // fixed-layer rects against the VISUAL viewport once the keyboard
        // offsets it, and subtracting `offsetTop` as well collapsed the panel
        // to a 14px sliver. See composerPopoverHeight.js.
        triggerBottom,
        viewportTop,
        viewportHeight,
        clipTop,
      }))
    }
    measure()
    // The keyboard animates in/out, and iOS scrolls the layout viewport during
    // that animation, so re-measure on every viewport event for as long as the
    // panel is open rather than trusting the open-time measurement.
    window.addEventListener('resize', measure)
    window.visualViewport?.addEventListener('resize', measure)
    window.visualViewport?.addEventListener('scroll', measure)
    // The textarea can grow while this stays open. That moves the trigger
    // upward without resizing either viewport, so observe the owning form too.
    const resizeObserver = typeof ResizeObserver !== 'undefined'
      ? new ResizeObserver(measure)
      : null
    const form = triggerRef.current?.closest('.chat__form')
    if (form) resizeObserver?.observe(form)
    return () => {
      window.removeEventListener('resize', measure)
      window.visualViewport?.removeEventListener('resize', measure)
      window.visualViewport?.removeEventListener('scroll', measure)
      resizeObserver?.disconnect()
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    function onPointer(e) {
      if (!wrapRef.current) return
      if (wrapRef.current.contains(e.target)) return
      setOpen(false)
    }
    function onKey(e) {
      if (e.key === 'Escape') {
        setOpen(false)
        // Return focus to the trigger so keyboard users don't get
        // stranded on document.body after Escape.
        triggerRef.current?.focus()
      }
    }
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  function handleAttach() {
    setOpen(false)
    // Refocus the chat textarea ONLY if the keyboard was already
    // up when the popover opened. Otherwise leave focus alone —
    // tapping + on a closed-keyboard chat shouldn't pop it open.
    if (wasInputFocusedRef.current) {
      focusComposerElement(composerInputRef?.current)
    }
    onAttachClick()
  }

  function handleOpenInspector() {
    setOpen(false)
    onOpenInspector?.()
  }

  function handleOpenSummary() {
    setOpen(false)
    onOpenSummary?.()
  }

  return (
    <div className="composer-plus" ref={wrapRef}>
      <button
        ref={triggerRef}
        type="button"
        className={`chat__plus${triggerClassName ? ` ${triggerClassName}` : ''}`
          + `${pending ? ' chat__plus--pending' : ''}`
          + `${open && !pending ? ' chat__plus--active' : ''}`}
        disabled={pending}
        // PointerDown preventDefault stops the focus from moving off
        // the textarea — keeps the soft keyboard open when the user
        // taps `+` mid-typing. Without this, focus shifts to the
        // button, the textarea blurs, and the keyboard collapses
        // before the popover even renders.
        onPointerDown={(e) => e.preventDefault()}
        onClick={() => {
          // Capture focus state at the click moment — before React
          // commits and any iOS focus-shuffling completes. See
          // wasInputFocusedRef declaration above.
          const el = composerInputRef?.current
          const wasFocused = document.activeElement === el
          if (!open) wasInputFocusedRef.current = wasFocused
          setOpen(o => !o)
          // Belt-and-suspenders against Android Chrome's focus
          // restoration: when the popover mounts as a sibling of
          // the pill, Chrome occasionally hands focus back to the
          // nearest input — popping the soft keyboard even though
          // `pointerdown.preventDefault()` should have prevented
          // any focus shift from the `+` tap. If the textarea was
          // NOT focused at tap-time, force it back unfocused on
          // the next frame.
          if (!wasFocused && el) {
            requestAnimationFrame(() => {
              if (document.activeElement === el) el.blur()
            })
          }
        }}
        aria-label={pending
          ? 'Chat options unavailable until this chat is ready'
          : triggerAriaLabel}
        aria-haspopup={pending ? undefined : 'dialog'}
        aria-expanded={pending ? undefined : open}
      >
        {triggerIcon}
      </button>
      {open && !pending && (
        <div
          className="composer-popover"
          role="dialog"
          aria-label={dialogAriaLabel}
          onPointerDown={preservePickerInputFocus}
          style={maxHeight !== null ? { maxHeight: `${maxHeight}px` } : undefined}
        >
          {showAttachAndContext && (
          <div className="composer-popover__section">
            <button
              type="button"
              className="composer-popover__row"
              onClick={handleAttach}
            >
              <span className="composer-popover__row-icon"><Paperclip width={20} height={20} /></span>
              <span className="composer-popover__row-main">
                <span className="composer-popover__row-title">Attach files</span>
                <span className="composer-popover__row-sub">
                  Images, PDFs, code
                </span>
              </span>
            </button>
          </div>
          )}
          {showModel && chatInfo && chatId && (
            <div className="composer-popover__section composer-popover__section--picker">
              <ChatSettingsPanel
                chatId={chatId}
                chat={chatInfo}
                provider={chatInfo.provider}
                effective={chatInfo.effective}
                hasAssistantTurns={hasAssistantTurns}
                autoResumeEnabled={autoResumeEnabled}
                autoResumeSaving={autoResumeSaving}
                autoResumeError={autoResumeError}
                onAutoResumeChange={onAutoResumeChange}
                restartResumeEnabled={restartResumeEnabled}
                restartResumeSaving={restartResumeSaving}
                restartResumeError={restartResumeError}
                onRestartResumeChange={onRestartResumeChange}
                onChange={onChangeChatInfo}
                providerSwitchState={providerSwitchState}
                settingsSaveTailRef={settingsSaveTailRef}
              />
            </div>
          )}
          {showAttachAndContext && !embedded && (
          <div className="composer-popover__section composer-popover__section--context">
            <button
              type="button"
              className="composer-popover__row"
              onClick={handleOpenSummary}
            >
              <span className="composer-popover__row-icon" aria-hidden="true">
                <FileDocument width={18} height={18} />
              </span>
              <span className="composer-popover__row-main">
                <span className="composer-popover__row-title">Chat summary</span>
                <span className="composer-popover__row-sub">
                  Name, digest, full handoff
                </span>
              </span>
            </button>
            <button
              type="button"
              className="composer-popover__row"
              onClick={handleOpenInspector}
            >
              <span className="composer-popover__row-icon" aria-hidden="true">
                <InfoCircle width={18} height={18} />
              </span>
              <span className="composer-popover__row-main">
                <span className="composer-popover__row-title">What the agent knows</span>
                <span className="composer-popover__row-sub">
                  System prompt and recent chats
                </span>
              </span>
            </button>
          </div>
          )}
        </div>
      )}
    </div>
  )
}
