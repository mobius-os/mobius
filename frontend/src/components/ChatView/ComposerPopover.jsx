/**
 * ComposerPopover — the brain button in the chat composer and the popover
 * it opens. Four sections in one popover:
 *
 *   1. Attach files + chat changes — calls `onAttachClick` (parent owns the hidden
 *      <input type="file"> so it can clear .value after each pick).
 *   2. Apps built here and artifacts touched by this chat.
 *   3. Model / effort / summary / automation — renders
 *      <ChatSettingsPanel> when a chatInfo is available; omitted on a fresh
 *      empty chat where chatInfo hasn't loaded yet.
 *   4. Chat summary / agent context — opens the two owner-facing continuity
 *      viewers after the picker.
 *
 * Open/close state, outside-click, and Escape live here. The trigger
 * is positioned as a sibling of the pill in `.chat__form`. The popover
 * is absolutely positioned relative to `.composer-plus` (the legacy-named
 * wrapper around the brain button), which has `position: relative`. Don't
 * remove that `position: relative` thinking `.chat__form` is the
 * anchor — the form is only relative so other absolutely-positioned
 * children (none today) could anchor to it.
 *
 * A draft-first New Chat uses this same component with `pending` only until
 * its server row exists, then reuses the model picker with unrelated chat
 * actions omitted. Keeping both states here prevents the provisional composer
 * from maintaining a second lookalike button that can drift from the real
 * control.
 *
 * Soft-keyboard contract: opening or using this popover preserves whether the
 * owning textarea was focused. The brain trigger suppresses native button focus
 * and records that state synchronously. The popover has one bubbling
 * pointer boundary that suppresses descendant focus and restores the textarea
 * on the next frame only when it was focused before opening. That next-frame
 * repair covers mobile Safari dropping focus during the later click without
 * stealing keyboard focus from keyboard-operated controls. ChatInputBar owns
 * the separate native file-picker return. Opening with the keyboard down keeps
 * the defensive next-frame blur for Android's occasional focus restoration.
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  ChevronDown,
  Code,
  DollarCircle,
  FileDocument,
  InfoCircle,
  Paperclip,
} from '@openai/apps-sdk-ui/components/Icon'
import BrainUsageIcon from './BrainUsageIcon.jsx'
import ChatSettingsPanel from './ChatSettingsPanel.jsx'
import ArtifactPickerSection from './ArtifactPickerSection.jsx'
import {
  chatArtifactPickerItems,
  loadChatArtifacts,
} from './chatArtifacts.js'
import { apiFetch } from '../../api/client.js'
import { popoverMaxHeight, nearestClipTop } from './composerPopoverHeight.js'
import { focusComposerElement } from './composerFocusPolicy.js'
import {
  captureLayoutSpace,
  clientLengthToLayout,
  clientPointToLayout,
} from '../../lib/layoutSpace.js'
import useModelSelectionPopover from './hooks/useModelSelectionPopover.js'
import useDiscardUnconfirmedSwitchOnPickerClose from './hooks/useDiscardUnconfirmedSwitchOnPickerClose.js'
import { resolvedChatSettings } from './modelSelectionPolicy.js'
import './ChatWork.css'

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
  providerSwitchState,
  settingsSaveTailRef,
  composerInputRef,
  modelSelectionRequest = 0,
  onOpenInspector,
  onOpenSummary,
  onOpenChanges,
  artifactsAppId = null,
  onOpenArtifact,
  onOpenUsage,
  appArtifacts = [],
  onOpenAppArtifact,
  embedded = false,
  pending = false,
  triggerIcon = null,
  providerUsage = null,
  triggerAriaLabel = 'Chat options',
}) {
  const wrapRef = useRef(null)
  const triggerRef = useRef(null)
  // Tracks whether the chat textarea was focused at the moment the
  // popover opened. If yes, refocus after a picker action so the
  // soft keyboard stays open. If no (user tapped the brain with keyboard
  // down), don't refocus — popping the keyboard up unexpectedly is
  // worse than the textarea losing focus.
  //
  // Captured SYNCHRONOUSLY inside the brain button's onClick so we
  // read activeElement at the exact moment of the tap. A previous
  // version captured this in a useEffect on `[open]`, which fires
  // AFTER React commits — on iOS Safari the focus state can shift
  // between the click handler and the post-commit effect, leaving
  // the ref stale. Sync capture in onClick is reliable.
  const { open, setOpen, wasInputFocusedRef } = useModelSelectionPopover(
    modelSelectionRequest,
    composerInputRef,
  )
  const artifactsQuery = useQuery({
    queryKey: ['chat-work-artifacts', String(artifactsAppId || ''), String(chatId || '')],
    queryFn: ({ signal }) => loadChatArtifacts(
      artifactsAppId,
      chatId,
      { signal, request: apiFetch },
    ),
    enabled: Boolean(open && !embedded && artifactsAppId && chatId),
    staleTime: 0,
  })
  const chatArtifacts = artifactsQuery.data || []
  const artifactItems = chatArtifactPickerItems(appArtifacts, chatArtifacts)
  const latestArtifact = artifactItems[0] || null
  const otherArtifacts = artifactItems.slice(1)
  const [artifactsExpanded, setArtifactsExpanded] = useState(false)
  useDiscardUnconfirmedSwitchOnPickerClose(
    open,
    providerSwitchState?.status,
    chatId,
  )
  useEffect(() => {
    if (!open) setArtifactsExpanded(false)
  }, [open])
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
      // Dismissing by pressing outside must not drop the soft keyboard. If the
      // textarea was focused when the popover opened, suppress the focus change
      // this outside press would otherwise cause (which blurs the textarea and
      // collapses the keyboard), then restore focus next frame only if a later
      // click still steals it — mirroring the popover's own pointer boundary.
      // preventDefault on pointerdown keeps focus and caret without blocking
      // scrolling, which is governed by touch-action.
      if (wasInputFocusedRef.current) {
        e.preventDefault()
        requestAnimationFrame(() => {
          const el = composerInputRef?.current
          if (el && document.activeElement !== el) focusComposerElement(el)
        })
      }
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
  }, [open, composerInputRef, setOpen, wasInputFocusedRef])

  function handleAttach() {
    if (!onAttachClick) return
    setOpen(false)
    // Refocus the chat textarea ONLY if the keyboard was already
    // up when the popover opened. Otherwise leave focus alone —
    // tapping the brain on a closed-keyboard chat shouldn't pop it open.
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

  function handleOpenChanges() {
    setOpen(false)
    onOpenChanges?.()
  }

  function handleOpenArtifact(artifactId) {
    setOpen(false)
    onOpenArtifact?.(artifactId)
  }

  function handleOpenUsage() {
    setOpen(false)
    onOpenUsage?.()
  }

  function handleOpenAppArtifact(app) {
    setOpen(false)
    onOpenAppArtifact?.(app)
  }

  function handleOpenArtifactItem(item) {
    if (item.kind === 'app') {
      handleOpenAppArtifact(item.app)
      return
    }
    handleOpenArtifact(item.id)
  }

  function togglePopover() {
    const el = composerInputRef?.current
    const wasFocused = document.activeElement === el
    if (!open) wasInputFocusedRef.current = wasFocused
    setOpen(current => !current)
    if (!wasFocused && el) {
      requestAnimationFrame(() => {
        if (document.activeElement === el) el.blur()
      })
    }
  }

  return (
    <div className="composer-plus" ref={wrapRef}>
      <button
        ref={triggerRef}
        type="button"
        className={`chat__plus chat__brain-usage${pending ? ' chat__plus--pending' : ''}`
          + `${open && !pending ? ' chat__plus--active' : ''}`}
        disabled={pending}
        // PointerDown preventDefault stops the focus from moving off
        // the textarea — keeps the soft keyboard open when the user
        // taps the brain mid-typing. Without this, focus shifts to the
        // button, the textarea blurs, and the keyboard collapses
        // before the popover even renders.
        onPointerDown={(e) => e.preventDefault()}
        onClick={togglePopover}
        aria-label={pending
          ? 'Chat options unavailable until this chat is ready'
          : triggerAriaLabel}
        aria-haspopup={pending ? undefined : 'dialog'}
        aria-expanded={pending ? undefined : open}
      >
        {triggerIcon || <BrainUsageIcon />}
      </button>
      {open && !pending && (
        <div
          className="composer-popover"
          role="dialog"
          aria-label="Chat options"
          onPointerDown={preservePickerInputFocus}
          style={maxHeight !== null ? { maxHeight: `${maxHeight}px` } : undefined}
        >
          <div className="composer-popover__section">
            {onAttachClick && (
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
            )}
            {!embedded && onOpenChanges && (
              <button
                type="button"
                className="composer-popover__row"
                onClick={handleOpenChanges}
              >
                <span className="composer-popover__row-icon" aria-hidden="true">
                  <Code width={19} height={19} />
                </span>
                <span className="composer-popover__row-main">
                  <span className="composer-popover__row-title">Changes</span>
                  <span className="composer-popover__row-sub">View this chat’s file changes</span>
                </span>
              </button>
            )}
          </div>
          {!embedded && artifactsAppId && artifactsQuery.isLoading && !latestArtifact && (
            <div className="composer-popover__section composer-popover__section--artifacts">
              <span className="composer-popover__eyebrow">Latest artifact</span>
              <div className="composer-popover__row" role="status">
                <span className="composer-popover__row-icon" aria-hidden="true">
                  <FileDocument width={18} height={18} />
                </span>
                <span className="composer-popover__row-main">
                  <span className="composer-popover__row-title">Looking in this chat…</span>
                </span>
              </div>
            </div>
          )}
          {!embedded && artifactsAppId && artifactsQuery.isError && !latestArtifact && (
            <div className="composer-popover__section composer-popover__section--artifacts">
              <button
                type="button"
                className="composer-popover__row"
                onClick={() => artifactsQuery.refetch()}
              >
                <span className="composer-popover__row-icon" aria-hidden="true">
                  <FileDocument width={18} height={18} />
                </span>
                <span className="composer-popover__row-main">
                  <span className="composer-popover__row-title">Artifacts unavailable</span>
                  <span className="composer-popover__row-sub">Tap to try again</span>
                </span>
              </button>
            </div>
          )}
          {!embedded && latestArtifact && (
            <ArtifactPickerSection
              latestArtifact={latestArtifact}
              otherArtifacts={otherArtifacts}
              expanded={artifactsExpanded}
              onToggle={() => setArtifactsExpanded(value => !value)}
              onOpenArtifact={handleOpenArtifactItem}
              documentIcon={<FileDocument width={18} height={18} />}
              disclosureIcon={<ChevronDown width={15} height={15} aria-hidden="true" />}
            />
          )}
          {chatInfo && chatId && (
            <div className="composer-popover__section composer-popover__section--picker">
              <ChatSettingsPanel
                chatId={chatId}
                chat={chatInfo}
                provider={chatInfo.provider}
                effective={resolvedChatSettings(chatInfo)}
                hasAssistantTurns={hasAssistantTurns}
                autoResumeEnabled={autoResumeEnabled}
                autoResumeSaving={autoResumeSaving}
                autoResumeError={autoResumeError}
                onAutoResumeChange={onAutoResumeChange}
                onChange={onChangeChatInfo}
                providerSwitchState={providerSwitchState}
                settingsSaveTailRef={settingsSaveTailRef}
                providerUsage={providerUsage}
              />
            </div>
          )}
          {!embedded && (onOpenSummary || onOpenInspector || onOpenUsage) && (
          <div className="composer-popover__section composer-popover__section--context">
            {onOpenSummary && (
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
            )}
            {onOpenInspector && (
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
            )}
            {onOpenUsage && (
              <button
                type="button"
                className="composer-popover__row"
                onClick={handleOpenUsage}
              >
                <span className="composer-popover__row-icon" aria-hidden="true">
                  <DollarCircle width={18} height={18} />
                </span>
                <span className="composer-popover__row-main">
                  <span className="composer-popover__row-title">Usage &amp; reported cost</span>
                  <span className="composer-popover__row-sub">
                    Per-turn tokens and provider-reported cost
                  </span>
                </span>
              </button>
            )}
          </div>
          )}
        </div>
      )}
    </div>
  )
}
