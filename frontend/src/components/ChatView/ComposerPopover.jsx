/**
 * ComposerPopover — the `+` button in the chat composer and the popover
 * it opens. Two sections in one popover:
 *
 *   1. Attach files  — calls `onAttachClick` (parent owns the hidden
 *      <input type="file"> so it can clear .value after each pick).
 *   2. Model / effort — renders <ChatSettingsPanel> when a chatInfo
 *      is available; omitted on a fresh empty chat where chatInfo
 *      hasn't loaded yet.
 *
 * Open/close state, outside-click, and Escape live here. The trigger
 * is positioned as a sibling of the pill in `.chat__form`. The popover
 * is absolutely positioned relative to `.composer-plus` (the wrapper
 * around the `+` button), which has `position: relative`. Don't
 * remove that `position: relative` thinking `.chat__form` is the
 * anchor — the form is only relative so other absolutely-positioned
 * children (none today) could anchor to it.
 *
 * ╔════════════════════════════════════════════════════════════════╗
 * ║                                                                ║
 * ║   SOFT-KEYBOARD CONTRACT — the rule that took the longest      ║
 * ║                                                                ║
 * ║   Tapping `+` MUST NEVER change the soft-keyboard state.       ║
 * ║   Up stays up. Down stays down. This holds regardless of       ║
 * ║   which affordance inside the popover the user then taps —     ║
 * ║   Attach files, a model row, an effort stop, or close          ║
 * ║   (Escape / outside-tap). The same contract holds for the      ║
 * ║   file-picker round-trip and the chip × button.                ║
 * ║                                                                ║
 * ║   THREE INDEPENDENT GUARDS make this work:                     ║
 * ║                                                                ║
 * ║   1. `pointerdown.preventDefault()` on EVERY interactive       ║
 * ║      element inside the composer (the +, every popover row,    ║
 * ║      every effort stop, every model row, the chip ×).          ║
 * ║      Spec-correct browsers honour this and don't shift         ║
 * ║      focus on tap.                                             ║
 * ║                                                                ║
 * ║   2. `wasInputFocusedRef` captured SYNCHRONOUSLY inside the    ║
 * ║      `+` button's onClick (not in a post-commit useEffect —    ║
 * ║      that misses iOS Safari's focus-shuffle window). Refocus   ║
 * ║      after every picker action is GATED on this ref. If it     ║
 * ║      was false, no refocus runs.                               ║
 * ║                                                                ║
 * ║   3. Defensive `requestAnimationFrame` blur on the `+` tap     ║
 * ║      if the textarea was NOT focused at tap-time. Catches      ║
 * ║      Android Chrome's occasional focus-restoration that        ║
 * ║      preventDefault doesn't always cover.                      ║
 * ║                                                                ║
 * ║   Violating any one of these can pop the keyboard or drop      ║
 * ║   focus inappropriately. They are NOT redundant; each one      ║
 * ║   plugs a different platform's behaviour. Don't simplify       ║
 * ║   without re-verifying on iOS Safari AND Android Chrome.       ║
 * ║                                                                ║
 * ║   `reqIdRef` lives in THIS file (not ChatSettingsPanel) so     ║
 * ║   the stale-PATCH monotonic counter survives panel unmount.    ║
 * ║   A panel-local ref would reset between popover opens.         ║
 * ║                                                                ║
 * ╚════════════════════════════════════════════════════════════════╝
 */

import { useEffect, useRef, useState } from 'react'
import { Plus, Paperclip } from '@openai/apps-sdk-ui/components/Icon'
import ChatSettingsPanel from './ChatSettingsPanel.jsx'

export default function ComposerPopover({
  chatInfo,
  chatId,
  onAttachClick,
  onChangeChatInfo,
  // Live-derived in the parent: `chatInfo.has_assistant_turns` is
  // set once on mount and never refreshed when the running turn
  // finishes. Without the live override, the cross-provider lock
  // in ChatSettingsPanel would stay disengaged after the first
  // reply lands in the same session. Parent ORs the persisted
  // flag with a `messages.some(m => m.role === 'assistant')`
  // check and passes the result down.
  hasAssistantTurns,
}) {
  const [open, setOpen] = useState(false)
  const wrapRef = useRef(null)
  const triggerRef = useRef(null)
  // Monotonic PATCH request counter. Lives here (not in ChatSettingsPanel)
  // because the panel unmounts on popover close; a panel-local ref would
  // reset between opens and break the stale-response guard. See
  // ChatSettingsPanel's `reqIdRef` prop for the rationale.
  const reqIdRef = useRef(0)
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
      const el = document.querySelector('.chat__input')
      if (el) el.focus({ preventScroll: true })
    }
    onAttachClick()
  }

  return (
    <div className="composer-plus" ref={wrapRef}>
      <button
        ref={triggerRef}
        type="button"
        className={`chat__plus${open ? ' chat__plus--active' : ''}`}
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
          const el = document.querySelector('.chat__input')
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
        aria-label="Add attachment or change model"
        aria-haspopup="dialog"
        aria-expanded={open}
      >
        <Plus width={26} height={26} />
      </button>
      {open && (
        <div className="composer-popover" role="dialog">
          <div className="composer-popover__section">
            <button
              type="button"
              className="composer-popover__row"
              // Keep the textarea focused so the soft keyboard stays
              // open while the user picks from the popover.
              onPointerDown={(e) => e.preventDefault()}
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
          {chatInfo && chatId && (
            <div className="composer-popover__section composer-popover__section--picker">
              <ChatSettingsPanel
                chatId={chatId}
                provider={chatInfo.provider}
                effective={chatInfo.effective}
                hasAssistantTurns={hasAssistantTurns}
                onChange={onChangeChatInfo}
                reqIdRef={reqIdRef}
                wasInputFocusedRef={wasInputFocusedRef}
              />
            </div>
          )}
        </div>
      )}
    </div>
  )
}
