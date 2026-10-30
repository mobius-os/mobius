/**
 * ChatInputBar — the chat composer.
 *
 * Composable layout with two button slots so we can add new
 * affordances (model picker, skill runner, thinking-level toggle —
 * Claude.ai's "/" picker etc.) without touching ChatView. To add a
 * button: pass a React element via `leftButtons` or `rightButtons`.
 *
 *   <ChatInputBar
 *     leftButtons={[
 *       <ComposerPopover ... />,  // attach files + model picker
 *     ]}
 *     ...
 *   />
 *
 * The primary action (Send / Stop / Mic) auto-resolves from props —
 * it's part of the bar's identity, not a slot. The bar itself owns
 * the resolution so callers don't have to think about it.
 *
 * ╔══════════════════════════════════════════════════════════════════╗
 * ║                                                                  ║
 * ║   CONTRACTS — small but load-bearing                             ║
 * ║                                                                  ║
 * ║   1. AUTOSIZE THRESHOLD                                          ║
 * ║      Textarea sizing toggles `data-composer-tall` when           ║
 * ║      height > 45px. NOT 30 (single-line is ~31, fires every      ║
 * ║      keystroke), NOT 50 (lags two-line typing). 45 sits          ║
 * ║      safely between single-line and two-line. See ChatView.css   ║
 * ║      composer architecture invariant #7 for rationale.           ║
 * ║                                                                  ║
 * ║   2. FILE-PICKER FOCUS                                           ║
 * ║      `wasInputFocusedAtPickerOpenRef` is captured inside the     ║
 * ║      `attachTriggerRef` closure — BEFORE the OS picker steals    ║
 * ║      focus. ComposerPopover.handleAttach has already restored    ║
 * ║      textarea focus by then iff the keyboard was up before the   ║
 * ║      + tap, so the check is accurate. Refocus after pick is      ║
 * ║      GATED on this ref — unconditional refocus would pop the     ║
 * ║      keyboard up even when the user opened + with kb down.       ║
 * ║      The same restoration runs when the native picker is         ║
 * ║      cancelled, so returning empty-handed does not lose focus.   ║
 * ║                                                                  ║
 * ║   3. CHIP × BUTTON keeps the keyboard                            ║
 * ║      The remove-attachment × has `onPointerDown.preventDefault`  ║
 * ║      just like every other interactive composer element.         ║
 * ║      Without it, tapping × steals focus → iOS collapses kb.      ║
 * ║                                                                  ║
 * ║   4. ICONS COME FROM THE APPS-SDK-UI PACKAGE                     ║
 * ║      Primary action: `ArrowUp` (24) for send, filtered SDK       ║
 * ║      `Mic` (22) for idle, and `Stop` (28) while streaming.       ║
 * ║      The package ships these — don't substitute hand-rolled      ║
 * ║      paths.                                                       ║
 * ║                                                                  ║
 * ║   5. ATTACH CARD CLASSIFIER (`classifyFile`) drives the badge    ║
 * ║      colour (PDF red, DOC blue, others muted). `stripExt`        ║
 * ║      removes the trailing .ext for DISPLAY ONLY — the agent      ║
 * ║      receives the full original filename via the attachment      ║
 * ║      metadata. Card is uniform 96×96 (square, matching image).   ║
 * ║                                                                  ║
 * ║   6. SEND BUTTON has both onClick AND onTouchEnd; touchend       ║
 * ║      preventDefault is what makes "tap-and-go" send instantly    ║
 * ║      on iOS Safari without waiting for the 300ms click           ║
 * ║      synthesis. Don't remove either handler.                     ║
 * ║                                                                  ║
 * ║   7. ENTER / SHORTCUT SEND                                       ║
 * ║      `isTouchPrimary()` (shared lib/pointerPrimary.js, also used ║
 * ║      by the inline QA + queued editors) gates plain Enter.        ║
 * ║      Touch devices: Enter inserts a                              ║
 * ║      newline. Desktop: Enter sends or steers queued text.        ║
 * ║      Cmd/Ctrl+Enter fast-forwards composed text into a live      ║
 * ║      turn when possible, otherwise it sends normally.            ║
 * ║      Shift+Enter always inserts a newline.                        ║
 * ║                                                                  ║
 * ║   8. MESSAGE HISTORY AND QUEUED EDITS                            ║
 * ║      Up walks queued messages (newest first), then sent ones;    ║
 * ║      Down comes back to the draft. They switch only from the     ║
 * ║      top or bottom line; elsewhere the caret moves natively.     ║
 * ║      The draft, with its attached files, is kept until it is     ║
 * ║      sent: edits to browsed messages survive moves and Esc       ║
 * ║      drops them. Enter saves a queued message in place or sends  ║
 * ║      an earlier one again without the draft's files, then the    ║
 * ║      draft returns. The queue's pencil opens its message here    ║
 * ║      too. Modifier chords and IME composition stay native.       ║
 * ║                                                                  ║
 * ╚══════════════════════════════════════════════════════════════════╝
 */

import { useRef, useState, useEffect, useLayoutEffect } from 'react'
import FileChips from './FileChips.jsx'
import { ArrowUp, DoubleChevronRight, Stop } from '@openai/apps-sdk-ui/components/Icon'
import {
  composerHistoryDirection,
  composerHistoryKeyPlan,
  composerHistoryProbeReachedBoundary,
} from './composerHistory.js'
import useComposerHistory from './hooks/useComposerHistory.js'
import ComposerHistoryStrip, { composerHistoryKeyHint } from './ComposerHistoryStrip.jsx'
import {
  composerFormattingMarker,
  isPlainTextPasteShortcut,
  linkPasteText,
  resolveComposerEnterAction,
  wrapComposerSelection,
} from './composerShortcuts.js'
import { isTouchPrimary } from '../../lib/pointerPrimary.js'
import ComposerMicIcon from './ComposerMicIcon.jsx'
import SlashMenu from './SlashMenu.jsx'
import {
  applySlashCommand,
  matchSlashCommands,
  resolveSlashMenuKey,
  slashCommandIsAvailable,
  slashCommandUnavailableReason,
  visibleSlashCommands,
} from './slashCommands.js'
import FileMentionMenu from './FileMentionMenu.jsx'
import {
  applyFileMention,
  matchMentionFiles,
  mentionAgentPath,
  mentionQueryFor,
} from './fileMentions.js'
import { useQuery } from '@tanstack/react-query'
import { api, jsonOrThrow } from '../../api/client.js'
import { projectQueries } from '../../hooks/queries.js'
import { filePasteNeedsDefaultPrevented, pastedFiles } from './pasteUpload.js'
import { hasSendablePayload } from './composerSubmission.js'
import {
  assistantClipboardText,
  insertClipboardText,
  queueClipboardTextUndoably,
} from './markdownClipboard.js'
import {
  textareaUsesNativeSizing,
  textareaHasWrappedLines,
  syncComposerTallState,
} from './composerTextareaSizing.js'
import {
  focusComposerElement,
  placeCaretAtTextEnd,
} from './composerFocusPolicy.js'




/** The primary action button — Steer / Send / Stop / Mic —
 *  auto-resolved from the bar's input/sending/listening/uploading state.
 *
 *  When queued work exists (`showSteer`), the Stop square is swapped for a
 *  fast-forward button immediately — including the brief persistence
 *  round-trip. Its handler waits for that write before acting, so the semantic
 *  control does not flash Send → Stop → Steer while the server confirms it.
 *  Stop is NOT lost: clearing the queue (the tray's X) flips showSteer back to
 *  false and the Stop square returns, and while the composer has text the Send
 *  button (queue-another) still wins over both.
 *
 *  Send, Steer, and Stop are states of the same primary action. They
 *  deliberately share the `primary` key so React preserves the 40px action
 *  target. Its glyph stack stays mounted so CSS can finish the directional
 *  glyph before Stop appears, without timing state in React. Send → Steer
 *  remains immediate. Mic stays distinct as the idle input affordance. */
export function PrimaryActionGlyphs({ action }) {
  return (
    <span className={`chat__action-glyphs chat__action-glyphs--${action}`} aria-hidden="true">
      <ArrowUp className="chat__action-glyph chat__action-glyph--send" width={24} height={24} />
      <DoubleChevronRight className="chat__action-glyph chat__action-glyph--steer" width={20} height={20} />
      <Stop className="chat__action-glyph chat__action-glyph--stop" width={28} height={28} />
    </span>
  )
}

export function PrimaryAction({
  sending, listening, hasInput, hasUploading, showSteer, steerReady,
  submissionBlocked, questionBlocked, savesQueued = false, resendsHistory = false,
  onSubmit, onStop, onSteer, onToggleVoice,
}) {
  // A queued message open in the chat box is saved in place. That edits the
  // queue rather than sending, so a running turn, a waiting question card, and
  // the draft's own uploads leave the button to it; the save reports anything
  // that blocks it (an empty edit included) in the history strip.
  // A recalled sent message resends without the draft's files (`fromHistory`),
  // so those uploads don't block it either.
  const holdsQueuedEdit = savesQueued && !listening
  if (!holdsQueuedEdit && !questionBlocked && sending && !hasInput && showSteer) {
    return (
      <button
        key="primary"
        className="chat__action chat__steer"
        type="button"
        // Keep focus stable through pointerdown, then let ChatView dismiss the
        // keyboard only after the authoritative steer row is positioned.
        // Dispatching on touchend avoids waiting for Safari's synthesized click.
        onPointerDown={(e) => e.preventDefault()}
        onTouchEnd={(e) => { e.preventDefault(); onSteer() }}
        onClick={onSteer}
        aria-label="Send queued message now"
        aria-busy={!steerReady}
        disabled={!steerReady}
      >
        <PrimaryActionGlyphs action="steer" />
      </button>
    )
  }
  // A pending question is a protocol barrier, not queueable work. Keep the
  // draft editable, but let Stop outrank Send until the card is answered.
  if (!holdsQueuedEdit && (questionBlocked || (sending && !hasInput))) {
    return (
      <button
        key="primary"
        className="chat__action chat__stop"
        type="button"
        // Match Send's touch handling: the composer keeps focus on
        // pointerdown, then dispatches the action on touchend instead of
        // waiting for a synthesized click. Without this, a focused mobile
        // textarea can eat the first Stop tap while the keyboard settles.
        onPointerDown={(e) => e.preventDefault()}
        onTouchEnd={(e) => { e.preventDefault(); onStop() }}
        onClick={onStop}
        aria-label="Stop"
      >
        <PrimaryActionGlyphs action="stop" />
      </button>
    )
  }
  if (holdsQueuedEdit || (hasInput && !listening)) {
    return (
      <button
        key="primary"
        className="chat__action chat__send"
        type="button"
        // Keep the textarea focused until ChatView snapshots the scroll
        // position in doSend(). On touch browsers, the native focus shift from
        // textarea → button can collapse the keyboard before the handler runs;
        // that changes the viewport geometry and can invalidate an otherwise
        // eligible FOLLOW_BOTTOM submit. ChatView snapshots the complete
        // mode+geometry decision first, then explicitly blurs on touch devices.
        onPointerDown={(e) => e.preventDefault()}
        onTouchEnd={(e) => { e.preventDefault(); onSubmit(e) }}
        onClick={onSubmit}
        aria-label={holdsQueuedEdit ? 'Save queued message' : 'Send'}
        // Offline is deliberately NOT a disable here. A send composed offline is
        // recorded in the durable outbox (chatOutbox) and auto-replays on
        // reconnect, so the button must reach doSend just like the Enter path
        // (canSubmit ignores offline). Gating it on `offline` was a leftover
        // from the old online-only composer and silently swallowed offline taps.
        disabled={!holdsQueuedEdit && ((hasUploading && !resendsHistory) || submissionBlocked)}
      >
        <PrimaryActionGlyphs action="send" />
      </button>
    )
  }
  return (
    <button
      key="mic"
      className={`chat__action chat__mic ${listening ? 'chat__mic--active' : ''}`}
      type="button"
      onTouchEnd={(e) => { e.preventDefault(); onToggleVoice() }}
      onClick={onToggleVoice}
      aria-label={listening ? 'Stop recording' : 'Voice input'}
      disabled={submissionBlocked && !listening}
    >
      <ComposerMicIcon />
    </button>
  )
}


/**
 * The input bar. Composes:
 *   • File-upload chips (above the input row)
 *   • Left-slot buttons (file attach by default; future "/" picker)
 *   • Textarea (autosizes, Enter-to-send on desktop)
 *   • Right-slot buttons (none by default; reserved for future use)
 *   • Primary action (Send / Stop / Mic — auto-resolved)
 *
 * Props:
 *   input              — current textarea value
 *   onInputChange      — receives new string
 *   onInputIntent      — runs after the controlled draft accepts an edit
 *   onSubmit           — called with FormEvent | MouseEvent | TouchEvent and
 *                        `{ fromHistory }`: true when the box holds an
 *                        earlier message, whose send leaves the draft's
 *                        attached files in the composer (contract 8)
 *   onSubmitSteer      — submits composed text and immediately steers
 *                        it when a live turn can accept steering; same
 *                        second argument as onSubmit
 *   inputRef           — for caller to focus/blur (e.g. dismiss keyboard)
 *   sending            — agent is currently streaming
 *   listening          — voice input active
 *   listeningRef       — synchronous mirror of voice input state
 *   onManualVoiceEdit  — rebases live dictation onto an owner-edited value
 *   onToggleVoice      — mic button handler
 *   onStop             — stop button handler
 *   onSteer            — fast-forward handler (steer queued msgs into the
 *                        live turn). Shown in place of Stop while a turn
 *                        is streaming AND `showSteer` is true.
 *   showSteer          — true as soon as queued work exists for a live turn;
 *                        drives the Steer-vs-Stop identity without waiting for
 *                        the queue persistence round-trip.
 *   steerReady         — false only while a steer tap is already in flight.
 *   canRequestSteer    — true when the keyboard shortcut may ask the
 *                        existing steer handler to reconcile/steer queued
 *                        messages, even before the visual fast-forward gate
 *                        is ready.
 *   canSubmitSteer     — true when Cmd/Ctrl+Enter may submit the current
 *                        draft through the live-turn steer path.
 *   pendingFiles       — file upload chips state
 *   onAddFiles         — receives FileList from file picker
 *   onRemoveFile       — receives chip id
 *   leftButtons        — buttons rendered to the LEFT of the pill
 *                        (e.g., <ComposerPopover /> — owns its own
 *                        "+" trigger; the bar no longer ships a
 *                        built-in attach button)
 *   rightButtons       — extra buttons before the primary action
 *                        (reserved for future use)
 *   attachTriggerRef   — caller-owned React ref. The bar installs
 *                        `attachTriggerRef.current = () =>
 *                        fileInputRef.current?.click()` in a layout
 *                        effect, so the parent (e.g. ComposerPopover)
 *                        can trigger the hidden <input type="file">
 *                        without the bar shipping a paperclip button.
 *   attachmentsDisabled — suppresses file picker and pasted-file handling
 *                        while a draft-only surface has no server row yet.
 *   submissionBlocked  — true while an atomic provider handoff owns the
 *                        chat; drafting stays available but send/mic-start do
 *                        not race the transition.
 *   questionBlocked    — true while an unanswered owner question holds the
 *                        turn; the draft stays editable and Stop owns the
 *                        primary action until the card is answered.
 *   messageHistory     — sent owner messages as composerHistory entries,
 *                        oldest first (composerHistoryFromMessages).
 *   queuedMessages     — visible queued rows; ↑ and the pencil open them here.
 *   onSaveQueued       — the queue's save, (cid, text) → outcome string.
 *   draftAside         — { set, take }: keeps the draft stored while history
 *                        shows another message, and hands it back.
 *   queuedComposerRef  — caller-owned ref. The bar installs
 *                        `{ open(cid), setAside(cid) }` so the queue tray's
 *                        pencil and × reach the chat box.
 *   onQueuedInComposerChange — receives the cid of the queued message the
 *                        box is editing, or null, for the tray's tag.
 *   provider           — the chat's provider id ('claude' | 'codex'). Filters
 *                        the "/" menu to commands that actually dispatch on it;
 *                        provider-specific commands stay hidden while unknown.
 *
 * The bar does NOT own send state — ChatView's doSend handles that.
 * The bar's only job: composition + the Send/Stop/Mic resolution.
 */
export default function ChatInputBar({
  chatId,
  input,
  onInputChange,
  onInputIntent,
  onSubmit,
  onSubmitSteer,
  inputRef,
  sending,
  listening,
  listeningRef,
  onManualVoiceEdit,
  onToggleVoice,
  onStop,
  onSteer,
  canSteer,
  showSteer = canSteer,
  steerReady = true,
  canRequestSteer = canSteer,
  canSubmitSteer = canRequestSteer,
  sendFailure = null,
  notice = null,
  submissionBlocked = false,
  questionBlocked = false,
  pendingFiles,
  onAddFiles,
  onRemoveFile,
  leftButtons,
  rightButtons,
  attachTriggerRef,
  attachmentsDisabled = false,
  messageHistory = [],
  queuedMessages = [],
  onSaveQueued,
  draftAside,
  queuedComposerRef,
  onQueuedInComposerChange,
  provider,
  projectRef = null,
}) {
  const fileInputRef = useRef(null)
  const pendingComposerCaretRef = useRef(null)
  const historyProbeVersionRef = useRef(0)
  const pasteAsPlainTextRef = useRef(false)
  // Captures whether the textarea was focused at the moment the file
  // picker opened. Read by `handleFileSelect` to decide whether to
  // refocus the textarea after the picker closes — refocusing
  // unconditionally would pop the soft keyboard up even when the
  // keyboard was down before the `+` tap.
  const wasInputFocusedAtPickerOpenRef = useRef(false)

  // Slash-command menu state. The list itself is derived from the current
  // text every render rather than stored, so it can never disagree with what
  // the composer holds; only the highlight and an explicit dismissal are state.
  const [slashIndex, setSlashIndex] = useState(0)
  const [slashDismissed, setSlashDismissed] = useState(false)
  const [slashInputFocused, setSlashInputFocused] = useState(false)
  const slashCandidates = matchSlashCommands(input)
  const slashMatches = visibleSlashCommands(slashCandidates, {
    focused: slashInputFocused,
    dismissed: slashDismissed,
  })
  // Clamped rather than trusted: the list shrinks as the query narrows, and a
  // stale highlight one past the end would accept `undefined`.
  const slashActiveIndex = Math.min(slashIndex, Math.max(slashMatches.length - 1, 0))
  const slashListId = `slash-menu-${chatId}`
  const slashOptionId = `${slashListId}-active`
  const slashNames = slashCandidates.map((command) => command.name).join(',')

  // A narrowed query should highlight the new best match, not keep pointing at
  // wherever the user had arrowed to in the previous, longer list.
  useEffect(() => { setSlashIndex(0) }, [slashNames])

  // Escape silences the menu for the command being typed — not forever. Once
  // the composer leaves slash mode (or the query stops matching anything), the
  // next "/" gets a fresh menu.
  useEffect(() => {
    if (slashCandidates.length === 0) setSlashDismissed(false)
  }, [slashCandidates.length])

  // @-mention of project files, for chats that belong to a project. Mirrors
  // slash-menu mechanics: the token is derived from the text every render,
  // only highlight and dismissal are state, and the textarea keeps focus.
  const [mentionIndex, setMentionIndex] = useState(0)
  const [mentionDismissed, setMentionDismissed] = useState(false)
  const mention = projectRef ? mentionQueryFor(input) : null
  // The whole project's file list is fetched once per mention session (and
  // kept briefly fresh) so filtering is instant while the owner types.
  const mentionFilesQuery = useQuery({
    queryKey: projectQueries.keys.fileIndex(projectRef?.id),
    queryFn: async ({ signal }) => jsonOrThrow(
      await api.projects.files(projectRef.id, '', { signal, recursive: true }),
      'Project files failed:',
    ),
    enabled: !!projectRef && mention !== null,
    staleTime: 15_000,
  })
  const mentionMatches = (mention !== null && slashInputFocused && !mentionDismissed)
    ? matchMentionFiles(mention.query, mentionFilesQuery.data?.entries)
    : []
  const mentionActiveIndex = Math.min(mentionIndex, Math.max(mentionMatches.length - 1, 0))
  const mentionListId = `file-mention-${chatId}`
  const mentionOptionId = `${mentionListId}-active`
  const mentionClosed = mention === null
  useEffect(() => { setMentionIndex(0) }, [mention?.query])
  useEffect(() => {
    if (mentionClosed) setMentionDismissed(false)
  }, [mentionClosed])

  function acceptFileMention(file) {
    if (!file || !mention) return
    const next = applyFileMention(input, mention, mentionAgentPath(projectRef?.root_path, file.path))
    noteComposerEdit()
    if (listeningRef?.current) onManualVoiceEdit?.(next)
    onInputChange(next)
    focusComposerElement(inputRef?.current)
    requestAnimationFrame(() => placeCaretAtTextEnd(inputRef?.current))
  }

  // Expose the hidden-file-input trigger to the parent. The parent
  // owns the visible "attach" affordance (now part of ComposerPopover);
  // the bar still owns the <input type="file"> so it can clear .value
  // after each pick. A layout effect keeps the ref pointed at the
  // live click-handler across re-renders without needing a stable
  // callback identity from the caller.
  useLayoutEffect(() => {
    if (!attachTriggerRef || attachmentsDisabled) return
    attachTriggerRef.current = () => {
      // Read focus state synchronously BEFORE the picker steals it.
      // ComposerPopover already restored focus to the textarea by
      // this point if-and-only-if it was focused before the popover
      // opened, so this check accurately reflects the user's
      // intended keyboard state.
      wasInputFocusedAtPickerOpenRef.current = (
        document.activeElement === inputRef?.current
      )
      fileInputRef.current?.click()
    }
    return () => {
      if (attachTriggerRef.current) attachTriggerRef.current = null
    }
  }, [attachTriggerRef, attachmentsDisabled, inputRef])

  // Typing, dictation, paste, and the menus edit whatever the box holds — the
  // draft or a browsed message — without leaving history (contract 8).
  function noteComposerEdit() {
    historyProbeVersionRef.current += 1
    pendingComposerCaretRef.current = null
    composerHistory.noteEdit()
  }

  // History moves, queued saves, and the returning draft all put text in the
  // box through this one path, with the same voice rebase and controlled
  // caret handoff as other programmatic edits.
  function showComposerValue(value) {
    historyProbeVersionRef.current += 1
    pendingComposerCaretRef.current = { value, caret: value.length }
    if (listeningRef?.current) {
      onManualVoiceEdit?.(value)
    }
    onInputChange(value)
    // Moving between entries can resolve to the value already shown, so React
    // may not commit a render for the caret layout effect below.
    const textarea = inputRef?.current
    if (textarea && textarea.value === value) {
      try { textarea.setSelectionRange(value.length, value.length) } catch {}
      pendingComposerCaretRef.current = null
    }
  }

  // The hook resets itself when the chat changes, so a traversal or its saved
  // draft never carries into another chat. History list refreshes do not
  // reset it: a queued message can start while the owner is browsing, and that
  // must not erase the draft that Down will restore.
  const composerHistory = useComposerHistory({
    chatId,
    value: input,
    sentHistory: messageHistory,
    queuedMessages,
    showValue: showComposerValue,
    saveQueued: onSaveQueued,
    draftAside,
  })
  const { open: openInComposer, setAside: setAsideFromComposer } = composerHistory

  // The queue tray is ChatView's sibling of this bar; its pencil and × reach
  // the box through this caller-owned ref, like attachTriggerRef above.
  useLayoutEffect(() => {
    if (!queuedComposerRef) return undefined
    queuedComposerRef.current = {
      open: (cid) => {
        openInComposer(cid)
        // Focus inside the tap so iOS keeps (or raises) the soft keyboard.
        focusComposerElement(inputRef?.current)
      },
      setAside: setAsideFromComposer,
    }
    return () => { queuedComposerRef.current = null }
  }, [queuedComposerRef, openInComposer, setAsideFromComposer, inputRef])

  // The tray tags the row open here; a chat box that goes away takes its
  // tag with it.
  useEffect(() => {
    onQueuedInComposerChange?.(composerHistory.queuedKey)
    return () => onQueuedInComposerChange?.(null)
  }, [composerHistory.queuedKey, onQueuedInComposerChange])

  // Programmatic composer edits arrive through the controlled value boundary.
  // Restore their caret after React commits without changing focus or scroll.
  useLayoutEffect(() => {
    const pending = pendingComposerCaretRef.current
    const textarea = inputRef?.current
    if (!pending || pending.value !== input || !textarea) return
    try { textarea.setSelectionRange(pending.anchor ?? pending.caret, pending.caret) } catch {}
    pendingComposerCaretRef.current = null
  }, [input, inputRef])

  // Modern browsers size the textarea from CSS (`field-sizing: content`).
  // Observe the resulting box rather than measuring scrollHeight on every
  // character; the pill alignment changes only when the textarea really
  // crosses from one visual line to multiple lines.
  useLayoutEffect(() => {
    const textarea = inputRef?.current
    if (
      !textarea
      || !textareaUsesNativeSizing()
      || typeof ResizeObserver === 'undefined'
    ) return undefined
    const observer = new ResizeObserver(entries => {
      const entry = entries[0]
      const borderSize = Array.isArray(entry?.borderBoxSize)
        ? entry.borderBoxSize[0]?.blockSize
        : entry?.borderBoxSize?.blockSize
      syncComposerTallState(textarea, borderSize ?? textarea.offsetHeight)
    })
    observer.observe(textarea)
    return () => observer.disconnect()
  }, [chatId, inputRef])

  // A completed attachment is a complete message in its own right. Feed that
  // through the same primary-action and keyboard-shortcut gate as typed text
  // so an image/file-only draft exposes Send instead of Mic.
  const hasInput = hasSendablePayload(input, pendingFiles)
  const hasUploading = pendingFiles?.some(c => c.status === 'uploading') ?? false

  function restoreFocusAfterFilePicker() {
    const shouldRestore = wasInputFocusedAtPickerOpenRef.current
    wasInputFocusedAtPickerOpenRef.current = false
    // The OS picker temporarily replaces the page. Restore the exact state the
    // owner had before opening it, including when they cancel without choosing
    // a file (modern browsers emit `cancel` instead of `change` in that case).
    if (shouldRestore) {
      setTimeout(() => focusComposerElement(inputRef?.current), 0)
    }
  }

  function handleFileSelect(e) {
    const fileList = Array.from(e.target.files || [])
    e.target.value = ''
    if (fileList.length) onAddFiles?.(fileList)
    restoreFocusAfterFilePicker()
  }

  function handleTextareaChange(e) {
    const value = e.target.value
    noteComposerEdit()
    if (listeningRef?.current) onManualVoiceEdit?.(value)
    onInputChange(value)
    onInputIntent?.(e.nativeEvent)
  }

  function handlePaste(e) {
    const preferPlainText = pasteAsPlainTextRef.current
    pasteAsPlainTextRef.current = false
    const files = attachmentsDisabled ? [] : pastedFiles(e.clipboardData)
    if (files.length > 0) {
      if (filePasteNeedsDefaultPrevented(e.clipboardData, files)) {
        e.preventDefault()
      }
      onAddFiles?.(files)
      return
    }

    const textarea = e.currentTarget
    const selectionStart = textarea.selectionStart
    const selectionEnd = textarea.selectionEnd
    // A web address pasted over selected text links that text; the plain-text
    // chord (Cmd/Ctrl+Shift+V) always pastes exactly what was copied.
    const linkText = preferPlainText ? null : linkPasteText(
      textarea.value.slice(selectionStart, selectionEnd),
      e.clipboardData?.getData?.('text/plain'),
    )
    const pastedText = linkText ?? assistantClipboardText(
      e.clipboardData,
      preferPlainText,
    )
    if (pastedText === null) return

    e.preventDefault()
    const pasteEvent = e.nativeEvent
    const applyControlledFallback = () => {
      // Some embedded browsers reject the native editing command. Keep the
      // controlled fallback so Markdown paste itself still works there.
      const next = insertClipboardText(
        input,
        selectionStart,
        selectionEnd,
        pastedText,
      )
      noteComposerEdit()
      pendingComposerCaretRef.current = next
      if (listeningRef?.current) onManualVoiceEdit?.(next.value)
      onInputChange(next.value)
      onInputIntent?.(pasteEvent)
    }

    if (queueClipboardTextUndoably(textarea, pastedText, applyControlledFallback)) return
    applyControlledFallback()
  }

  // Cmd/Ctrl+B, I and E wrap the selection through the same undo-safe edit as
  // paste, then select the wrapped text (or its placeholder) to type over.
  function applyComposerFormatting(textarea, marker) {
    const edit = wrapComposerSelection(
      textarea.value,
      textarea.selectionStart,
      textarea.selectionEnd,
      marker,
    )
    const selectWrappedText = () => {
      try { textarea.setSelectionRange(edit.selectionStart, edit.selectionEnd) } catch {}
    }
    const applyControlledFallback = () => {
      noteComposerEdit()
      pendingComposerCaretRef.current = {
        value: edit.value,
        anchor: edit.selectionStart,
        caret: edit.selectionEnd,
      }
      if (listeningRef?.current) onManualVoiceEdit?.(edit.value)
      onInputChange(edit.value)
    }
    if (queueClipboardTextUndoably(
      textarea, edit.text, applyControlledFallback, selectWrappedText,
    )) return
    applyControlledFallback()
  }

  function acceptSlashCommand(command) {
    if (!command || !slashCommandIsAvailable(command, provider)) return
    const value = applySlashCommand(command)
    noteComposerEdit()
    if (listeningRef?.current) onManualVoiceEdit?.(value)
    onInputChange(value)
    // The textarea never lost focus (rows suppress pointerdown), but a click
    // accept still needs the caret put back after the controlled update.
    focusComposerElement(inputRef?.current)
    requestAnimationFrame(() => placeCaretAtTextEnd(inputRef?.current))
  }

  const canSubmit = !submissionBlocked && !questionBlocked

  function handleKeyDown(e) {
    pasteAsPlainTextRef.current = isPlainTextPasteShortcut(e)
    // The menus claim Enter and the arrows while open — the same keys that
    // otherwise send and walk sent-message history — so they resolve BEFORE
    // both. Keys they don't claim fall through untouched. Mention and slash
    // modes are mutually exclusive (a leading "/" cannot also end in an
    // "@token"), so ordering between them never decides a key.
    const mentionAction = resolveSlashMenuKey(e, {
      open: mentionMatches.length > 0,
      count: mentionMatches.length,
    })
    if (mentionAction) {
      e.preventDefault()
      const total = mentionMatches.length
      if (mentionAction === 'dismiss') setMentionDismissed(true)
      else if (mentionAction === 'next') setMentionIndex((i) => (i + 1) % total)
      else if (mentionAction === 'previous') setMentionIndex((i) => (i - 1 + total) % total)
      else if (mentionAction === 'accept') acceptFileMention(mentionMatches[mentionActiveIndex])
      return
    }

    const slashAction = resolveSlashMenuKey(e, {
      open: slashMatches.length > 0,
      count: slashMatches.length,
    })
    if (slashAction) {
      e.preventDefault()
      const total = slashMatches.length
      if (slashAction === 'dismiss') setSlashDismissed(true)
      else if (slashAction === 'next') setSlashIndex((i) => (i + 1) % total)
      else if (slashAction === 'previous') setSlashIndex((i) => (i - 1 + total) % total)
      else if (slashAction === 'accept') acceptSlashCommand(slashMatches[slashActiveIndex])
      return
    }

    const formattingMarker = composerFormattingMarker(e)
    if (formattingMarker) {
      e.preventDefault()
      applyComposerFormatting(e.currentTarget, formattingMarker)
      return
    }

    // Esc gives the box back to the draft and drops kept edits. Outside
    // history it stays native; the menus above already claimed theirs.
    if (
      e.key === 'Escape'
      && composerHistory.browsing
      && !e.altKey && !e.ctrlKey && !e.metaKey && !e.shiftKey
      && !e.isComposing && !e.nativeEvent?.isComposing
    ) {
      e.preventDefault()
      composerHistory.escape()
      return
    }

    const historyDirection = composerHistoryDirection(e)
    const historyPlan = historyDirection && composerHistoryKeyPlan(e, {
      direction: historyDirection,
      value: input,
      // Only ↑ uses it, and only a non-empty box needs the layout read.
      linesWrap: historyDirection !== 'older' || !input
        || textareaHasWrappedLines(e.target),
    })
    if (historyPlan?.when === 'now') {
      if (composerHistory.move(historyDirection)) e.preventDefault()
      return
    }
    const canMoveHistory = historyDirection === 'older'
      ? composerHistory.hasEntries
      : composerHistory.browsing
    if (historyPlan?.when === 'after-native' && canMoveHistory) {
      // Let the browser move the caret first; switch only if it was already
      // on the edge line. A later edit or move invalidates this probe.
      const probe = historyPlan.probe
      const probeVersion = ++historyProbeVersionRef.current
      requestAnimationFrame(() => {
        if (
          historyProbeVersionRef.current !== probeVersion
          || !composerHistoryProbeReachedBoundary(probe, inputRef?.current)
        ) return
        composerHistory.move(historyDirection)
      })
      return
    }

    const action = resolveComposerEnterAction(e, {
      hasInput,
      canSteer,
      canRequestSteer,
      canSubmitSteer,
      isTouchPrimary: isTouchPrimary(),
    })
    if (!action) return
    e.preventDefault()
    if (action === 'submit' || action === 'submit-steer') {
      submitComposer(e, { steer: action === 'submit-steer' })
      return
    }
    // An empty box asks to steer queued work — unless a message is open here:
    // then Enter belongs to that message (an empty queued edit explains why
    // it can't be saved).
    if (composerHistory.browsing) {
      if (composerHistory.position?.kind === 'queued') composerHistory.submit()
      return
    }
    if (action === 'steer') onSteer()
  }

  // Enter and the send button. A queued message open here is saved in place
  // of the original; anything else is sent as a new message, and a draft set
  // aside by history comes back once that send clears the box. The draft's
  // attached files belong to the draft, so an earlier message sent again from
  // history goes without them (`fromHistory`).
  function submitComposer(e, { steer = false } = {}) {
    const savesQueued = composerHistory.position?.kind === 'queued'
    if (!savesQueued && !canSubmit) {
      e?.preventDefault?.()
      return
    }
    const fromHistory = composerHistory.browsing
    if (composerHistory.submit() === 'handled') {
      e?.preventDefault?.()
      return
    }
    if (steer) onSubmitSteer(e, { fromHistory })
    else onSubmit(e, { fromHistory })
  }

  function handleSubmit(e) {
    submitComposer(e)
  }

  const hasFiles = !!pendingFiles?.length
  const touchPrimary = isTouchPrimary()
  const historyKeyHint = composerHistoryKeyHint({
    browsing: composerHistory.browsing,
    hasEntries: composerHistory.hasEntries,
    touch: touchPrimary,
  })
  const historyKeyHintId = `composer-history-keys-${chatId}`

  return (
    <form className="chat__form" onSubmit={handleSubmit}>
      {!attachmentsDisabled && (
        <input
          type="file"
          multiple
          ref={fileInputRef}
          onChange={handleFileSelect}
          onCancel={restoreFocusAfterFilePicker}
          style={{ display: 'none' }}
        />
      )}
      {notice && (
        <div
          className="chat__offline-note"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {notice}
        </div>
      )}
      {sendFailure && (
        <div
          className="chat__offline-note chat__offline-note--error"
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          {sendFailure}
        </div>
      )}
      <SlashMenu
        commands={slashMatches}
        activeIndex={slashActiveIndex}
        onSelect={acceptSlashCommand}
        isAvailable={(command) => slashCommandIsAvailable(command, provider)}
        unavailableReason={(command) => slashCommandUnavailableReason(command, provider)}
        listId={slashListId}
        optionId={slashOptionId}
      />
      <FileMentionMenu
        files={mentionMatches}
        activeIndex={mentionActiveIndex}
        onSelect={acceptFileMention}
        listId={mentionListId}
        optionId={mentionOptionId}
      />
      <ComposerHistoryStrip
        position={composerHistory.position}
        note={composerHistory.note}
        saving={composerHistory.saving}
        draftFileCount={pendingFiles?.length || 0}
        touch={touchPrimary}
        onBack={composerHistory.escape}
      />
      <div className="chat__input-row">
        {leftButtons}
        <div className={`chat__pill${hasFiles ? ' chat__pill--with-attach' : ''}${hasFiles && composerHistory.browsing ? ' chat__pill--draft-aside' : ''}`}>
          {hasFiles && (
            <FileChips
              files={pendingFiles}
              onRemove={onRemoveFile}
              chatId={chatId}
            />
          )}
          <div className="chat__input-line">
            <textarea
              ref={inputRef}
              className="chat__input"
              value={input}
              onChange={handleTextareaChange}
              onPaste={handlePaste}
              onKeyDown={handleKeyDown}
              onKeyUp={() => { pasteAsPlainTextRef.current = false }}
              // Native editing owns selection; explicit app-driven focus
              // still places the caret through focusComposerElement.
              onFocus={() => setSlashInputFocused(true)}
              onBlur={() => setSlashInputFocused(false)}
              placeholder="Message Möbius…"
              aria-label="Message Möbius…"
              aria-describedby={historyKeyHint ? historyKeyHintId : undefined}
              name="message"
              autoComplete="off"
              spellCheck={false}
              rows={1}
              // Combobox semantics apply only while the menu is open. Left on
              // permanently they would announce this plain prose textarea as a
              // picker in every ordinary message the user writes.
              {...(slashMatches.length > 0 || mentionMatches.length > 0 ? {
                role: 'combobox',
                'aria-expanded': true,
                'aria-controls': slashMatches.length > 0 ? slashListId : mentionListId,
                'aria-activedescendant': slashMatches.length > 0 ? slashOptionId : mentionOptionId,
                'aria-autocomplete': 'list',
              } : {})}
            />
            {historyKeyHint && (
              <span id={historyKeyHintId} className="chat__sr-only">{historyKeyHint}</span>
            )}
            {rightButtons}
            <PrimaryAction
              sending={sending}
              listening={listening}
              hasInput={hasInput}
              hasUploading={hasUploading}
              showSteer={showSteer}
              steerReady={steerReady}
              submissionBlocked={submissionBlocked}
              questionBlocked={questionBlocked}
              savesQueued={composerHistory.position?.kind === 'queued'}
              resendsHistory={composerHistory.browsing}
              onSubmit={handleSubmit}
              onStop={onStop}
              onSteer={onSteer}
              onToggleVoice={onToggleVoice}
            />
          </div>
        </div>
      </div>
    </form>
  )
}
