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
 * ║      Shared textarea sizing toggles `chat__pill--tall` when     ║
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
 * ║      Primary action: `ArrowUp` (22) for send, `Mic` (24), and    ║
 * ║      `Stop` (28). The package ships these — don't substitute     ║
 * ║      hand-rolled paths.                                          ║
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
 * ║      `_isTouchPrimary` is detected once via                      ║
 * ║      `matchMedia('(hover: none) and (pointer: coarse)')` and     ║
 * ║      gates plain Enter. Touch devices: Enter inserts a           ║
 * ║      newline. Desktop: Enter sends or steers queued text.        ║
 * ║      Cmd/Ctrl+Enter fast-forwards composed text into a live      ║
 * ║      turn when possible, otherwise it sends normally.            ║
 * ║      Shift+Enter always inserts a newline.                        ║
 * ║                                                                  ║
 * ║   8. SENT-MESSAGE HISTORY                                        ║
 * ║      Up recalls sent messages only after native textarea movement ║
 * ║      has reached the visual top; once browsing, Up/Down walk       ║
 * ║      history and Down past the newest restores the unfinished     ║
 * ║      draft. Manual edits and sends exit history. Modifier chords   ║
 * ║      and IME composition stay native.                              ║
 * ║                                                                  ║
 * ╚══════════════════════════════════════════════════════════════════╝
 */

import { useRef, useState, useEffect, useLayoutEffect } from 'react'
import { createPortal } from 'react-dom'
import ImageLightbox from './markdown/ImageLightbox.jsx'
import { useHistoryDismiss } from '../../hooks/useHistoryDismiss.jsx'
import { ArrowUp, DoubleChevronRight, Mic, Stop } from '@openai/apps-sdk-ui/components/Icon'
import { BASE } from '../../api/client.js'
import { mediaTokenParam } from '../../api/mediaToken.js'
import {
  composerHistoryNativeProbe,
  composerHistoryProbeReachedBoundary,
  resolveComposerHistoryMove,
} from './composerHistory.js'
import { resolveComposerEnterAction } from './composerShortcuts.js'
import SlashMenu from './SlashMenu.jsx'
import {
  applySlashCommand,
  matchSlashCommands,
  resolveSlashMenuKey,
  slashCommandIsAvailable,
  slashCommandUnavailableReason,
  visibleSlashCommands,
} from './slashCommands.js'
import { filePasteNeedsDefaultPrevented, pastedFiles } from './pasteUpload.js'
import { hasSendablePayload } from './composerSubmission.js'
import {
  textareaUsesNativeSizing,
  syncComposerTallClass,
} from './composerTextareaSizing.js'
import { focusComposerElement } from './composerFocusPolicy.js'


// Detect touch-primary once (same heuristic ChatView uses).
const _touchMql = typeof matchMedia === 'function'
  ? matchMedia('(hover: none) and (pointer: coarse)')
  : null
let _isTouchPrimary = _touchMql?.matches ?? false
_touchMql?.addEventListener('change', (e) => { _isTouchPrimary = e.matches })


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
function PrimaryActionGlyphs({ action }) {
  return (
    <span className={`chat__action-glyphs chat__action-glyphs--${action}`} aria-hidden="true">
      <ArrowUp className="chat__action-glyph chat__action-glyph--send" width={22} height={22} />
      <DoubleChevronRight className="chat__action-glyph chat__action-glyph--steer" width={20} height={20} />
      <Stop className="chat__action-glyph chat__action-glyph--stop" width={28} height={28} />
    </span>
  )
}

function PrimaryAction({
  sending, listening, hasInput, hasUploading, offline, showSteer, steerReady,
  submissionBlocked,
  onSubmit, onStop, onSteer, onToggleVoice,
}) {
  if (sending && !hasInput && showSteer) {
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
  if (sending && !hasInput) {
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
  if (hasInput && !listening) {
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
        aria-label="Send"
        disabled={hasUploading || offline || submissionBlocked}
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
      <Mic width={24} height={24} />
    </button>
  )
}


/** File-upload chips (rendered above the input row when files exist). */
/** Classifies a file by extension into a colored badge variant.
 *  Returns {kind, label} where kind = 'pdf' | 'doc' | 'code' and
 *  label is the short tag shown inside the badge. */
function classifyFile(name) {
  const ext = (name.split('.').pop() || '').toLowerCase()
  if (ext === 'pdf') return { kind: 'pdf', label: 'PDF' }
  if (['doc', 'docx', 'rtf', 'odt'].includes(ext)) return { kind: 'doc', label: 'DOC' }
  if (['xls', 'xlsx', 'csv', 'tsv'].includes(ext)) return { kind: 'doc', label: 'XLS' }
  if (['ppt', 'pptx'].includes(ext)) return { kind: 'doc', label: 'PPT' }
  if (['md', 'markdown', 'txt'].includes(ext)) return { kind: 'doc', label: 'TXT' }
  if (['zip', 'tar', 'gz', 'rar', '7z'].includes(ext)) return { kind: 'doc', label: 'ZIP' }
  return { kind: 'code', label: (ext || 'FILE').toUpperCase().slice(0, 4) }
}

/** Strip the trailing `.ext` so the visible name reads like a label
 *  rather than a file. The badge already communicates the type
 *  (PDF / DOC / TXT / etc.), so the extension is redundant and just
 *  eats horizontal room on a fixed-width card. Leaves names
 *  without a dot untouched and
 *  preserves any earlier dots in the name (e.g. `report.v2.pdf`
 *  → `report.v2`). */
function stripExt(name) {
  if (!name) return name
  const idx = name.lastIndexOf('.')
  if (idx <= 0) return name
  return name.slice(0, idx)
}

/** Fixed-box attach cards rendered inside the pill above the input
 *  row when files are attached. Two variants:
 *   - image (PNG/JPEG/etc.): 72×72 square thumbnail; the image IS
 *     the identifier so no filename label.
 *   - file (PDF/DOC/code): 168px-wide rectangle with a colored
 *     type badge and the filename below.
 *  The remove `×` is a 20×20 button floating at the card's top-
 *  right corner (half-overlapping outside). */
function FileChips({ files, onRemove, chatId }) {
  const [tokenState, setTokenState] = useState({
    chatId: null,
    param: '',
    failed: false,
  })
  // Index into the attached-image gallery currently shown full-screen.
  const [lightboxIndex, setLightboxIndex] = useState(null)
  const historyDismiss = useHistoryDismiss(() => setLightboxIndex(null))
  const hasRestoredImage = files?.some(file => (
    file.mime_type?.startsWith('image/') && !file.objectUrl
  ))

  useEffect(() => {
    if (!hasRestoredImage || !chatId) {
      setTokenState({ chatId: null, param: '', failed: false })
      return undefined
    }
    let cancelled = false
    setTokenState({ chatId, param: '', failed: false })
    mediaTokenParam(chatId).then(param => {
      if (!cancelled) setTokenState({ chatId, param, failed: !param })
    }).catch(() => {
      if (!cancelled) setTokenState({ chatId, param: '', failed: true })
    })
    return () => { cancelled = true }
  }, [chatId, hasRestoredImage])

  // Never reuse a previous chat's token during the effect boundary.
  const currentTokenState = tokenState.chatId === chatId
    ? tokenState
    : { param: '', failed: false }

  if (!files?.length) return null

  const cards = files.map(chip => {
    const isImage = !!chip.objectUrl || chip.mime_type?.startsWith('image/')
    const previewSrc = chip.objectUrl || (
      isImage && currentTokenState.param
        ? `${BASE}/api/chats/${chatId}/uploads/${encodeURIComponent(chip.name)}${currentTokenState.param}`
        : ''
    )
    return {
      chip,
      isImage,
      previewSrc,
      previewFailed: !!(isImage && !chip.objectUrl && currentTokenState.failed),
    }
  })
  // Every viewable attached image, in tray order, so the full-screen
  // viewer can page/swipe between them like a sent-message gallery. Each
  // card records its own gallery position: two attachments can share a
  // preview URL (same restored filename), so looking the index up by src
  // would open the wrong one.
  const gallery = []
  for (const card of cards) {
    if (!card.isImage || !card.previewSrc) continue
    card.galleryIndex = gallery.length
    gallery.push({ src: card.previewSrc, alt: card.chip.name })
  }
  // Removing an attachment while open can invalidate the index; treat
  // an out-of-range index as closed rather than showing the wrong image.
  const openIndex = lightboxIndex !== null && lightboxIndex < gallery.length
    ? lightboxIndex
    : null

  return (
    <div className="chat__attach-tray">
      {cards.map(({ chip, isImage, previewSrc, previewFailed, galleryIndex }) => {
        const cls = classifyFile(chip.name || '')
        const errorMark = chip.status === 'error' ? ' chat__attach-card--error' : ''
        return (
          <div
            key={chip.id}
            className={
              'chat__attach-card'
              + (isImage ? ' chat__attach-card--image' : ' chat__attach-card--file')
              + errorMark
            }
            title={previewFailed
              ? `${chip.name} — preview unavailable; attachment is still ready to send`
              : (chip.status === 'error' ? chip.error : chip.name)}
          >
            {isImage && previewSrc ? (
              <button
                type="button"
                className="chat__attach-card-thumb-button"
                // Preserve the textarea until the dialog mounts. The shared
                // lightbox then moves focus into itself deliberately and
                // restores this button/text-entry context when it closes.
                onPointerDown={(e) => e.preventDefault()}
                onClick={() => {
                  historyDismiss.open()
                  setLightboxIndex(galleryIndex)
                }}
                aria-label={`View ${chip.name} full screen`}
              >
                <img className="chat__attach-card-thumb" src={previewSrc} alt="" />
              </button>
            ) : previewFailed ? (
              <span className="chat__attach-card-preview-error" role="status">
                Preview unavailable
              </span>
            ) : isImage ? (
              <span className="chat__attach-card-spin" aria-hidden="true" />
            ) : (
              <>
                <span className={`chat__attach-card-icon chat__attach-card-icon--${cls.kind}`}>
                  {cls.label}
                </span>
                <span className="chat__attach-card-name">{stripExt(chip.name)}</span>
              </>
            )}
            {chip.status === 'uploading' && (
              <span className="chat__attach-card-spin" aria-hidden="true" />
            )}
            <button
              type="button"
              className="chat__attach-card-remove"
              // Keep the soft keyboard up — without preventDefault on
              // pointerdown the tap shifts focus off the textarea and
              // iOS collapses the keyboard. Matches the same trick
              // used on the `+` trigger, the popover rows, and every
              // other interactive element inside the composer.
              onPointerDown={(e) => e.preventDefault()}
              onClick={() => onRemove(chip.id)}
              aria-label={`Remove ${chip.name}`}
            >×</button>
          </div>
        )
      })}
      {openIndex !== null && createPortal(
        <ImageLightbox
          src={gallery[openIndex].src}
          alt={gallery[openIndex].alt}
          items={gallery}
          index={openIndex}
          onNavigate={setLightboxIndex}
          onClose={historyDismiss.close}
        />,
        document.body,
      )}
    </div>
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
 *   onSubmit           — called with FormEvent | MouseEvent | TouchEvent
 *   onSubmitSteer      — submits composed text and immediately steers
 *                        it when a live turn can accept steering
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
 *   submissionBlocked  — true while an atomic provider handoff owns the
 *                        chat; drafting stays available but send/mic-start do
 *                        not race the transition.
 *   messageHistory     — visible owner-authored message text, oldest first.
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
  offline,
  sendFailure = null,
  submissionBlocked = false,
  pendingFiles,
  onAddFiles,
  onRemoveFile,
  leftButtons,
  rightButtons,
  attachTriggerRef,
  messageHistory = [],
  provider,
}) {
  const fileInputRef = useRef(null)
  const historyIndexRef = useRef(null)
  const historyDraftRef = useRef('')
  const historyCaretRef = useRef(null)
  const historyProbeVersionRef = useRef(0)
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

  // Expose the hidden-file-input trigger to the parent. The parent
  // owns the visible "attach" affordance (now part of ComposerPopover);
  // the bar still owns the <input type="file"> so it can clear .value
  // after each pick. A layout effect keeps the ref pointed at the
  // live click-handler across re-renders without needing a stable
  // callback identity from the caller.
  useLayoutEffect(() => {
    if (!attachTriggerRef) return
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
  }, [attachTriggerRef, inputRef])

  function resetMessageHistory() {
    historyProbeVersionRef.current += 1
    historyIndexRef.current = null
    historyDraftRef.current = ''
    historyCaretRef.current = null
  }

  // Never carry a traversal or its saved draft into another chat. History
  // list refreshes deliberately do not reset this: a queued message can move
  // into the transcript while the owner is browsing, and that must not erase
  // the draft that Down will restore.
  useEffect(() => {
    resetMessageHistory()
  }, [chatId])

  // History values arrive through the controlled composer boundary. Restore
  // the caret after React commits that value without changing focus or scroll.
  useLayoutEffect(() => {
    const pending = historyCaretRef.current
    const textarea = inputRef?.current
    if (!pending || pending.value !== input || !textarea) return
    try { textarea.setSelectionRange(pending.caret, pending.caret) } catch {}
    historyCaretRef.current = null
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
      syncComposerTallClass(
        textarea,
        borderSize ?? entry?.target?.getBoundingClientRect?.().height,
      )
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
    if (fileList.length) onAddFiles(fileList)
    restoreFocusAfterFilePicker()
  }

  function handleTextareaChange(e) {
    const value = e.target.value
    resetMessageHistory()
    if (listeningRef?.current) onManualVoiceEdit?.(value)
    onInputChange(value)
    onInputIntent?.(e.nativeEvent)
  }

  function handlePaste(e) {
    const files = pastedFiles(e.clipboardData)
    if (files.length === 0) return
    if (filePasteNeedsDefaultPrevented(e.clipboardData, files)) {
      e.preventDefault()
    }
    onAddFiles(files)
  }

  function acceptSlashCommand(command) {
    if (!command || !slashCommandIsAvailable(command, provider)) return
    const value = applySlashCommand(command)
    resetMessageHistory()
    if (listeningRef?.current) onManualVoiceEdit?.(value)
    onInputChange(value)
    // The textarea never lost focus (rows suppress pointerdown), but a click
    // accept still needs the caret put back after the controlled update.
    inputRef?.current?.focus({ preventScroll: true })
  }

  function handleKeyDown(e) {
    // The menu claims Enter and the arrows while it is open — the same keys
    // that otherwise send and walk sent-message history — so it resolves
    // BEFORE both. Keys it doesn't claim fall through untouched.
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

    function applyHistoryMove(historyMove) {
      historyIndexRef.current = historyMove.index
      historyDraftRef.current = historyMove.draft
      historyCaretRef.current = {
        value: historyMove.value,
        caret: historyMove.value.length,
      }
      if (listeningRef?.current) {
        onManualVoiceEdit?.(historyMove.value)
      }
      onInputChange(historyMove.value)
      // At the oldest entry another Up resolves to the same controlled value,
      // so React may not commit a render for the layout effect above.
      if (historyMove.value === input) {
        try {
          inputRef?.current?.setSelectionRange(
            historyMove.value.length,
            historyMove.value.length,
          )
        } catch {}
        historyCaretRef.current = null
      }
    }

    const historyMove = resolveComposerHistoryMove(e, {
      history: messageHistory,
      index: historyIndexRef.current,
      draft: historyDraftRef.current,
      value: input,
    })
    if (historyMove) {
      e.preventDefault()
      applyHistoryMove(historyMove)
      return
    }

    const historyProbe = composerHistoryNativeProbe(e, {
      history: messageHistory,
      index: historyIndexRef.current,
      value: input,
    })
    if (historyProbe) {
      const probeVersion = ++historyProbeVersionRef.current
      requestAnimationFrame(() => {
        const textarea = inputRef?.current
        if (
          historyProbeVersionRef.current !== probeVersion
          || !composerHistoryProbeReachedBoundary(historyProbe, textarea)
        ) return
        const deferredMove = resolveComposerHistoryMove({
          key: 'ArrowUp',
          target: textarea,
        }, {
          history: messageHistory,
          index: historyIndexRef.current,
          draft: historyDraftRef.current,
          value: input,
          nativeBoundary: true,
        })
        if (deferredMove) applyHistoryMove(deferredMove)
      })
      return
    }

    const action = resolveComposerEnterAction(e, {
      hasInput,
      canSteer,
      canRequestSteer,
      canSubmitSteer,
      isTouchPrimary: _isTouchPrimary,
    })
    if (!action) return
    e.preventDefault()
    if (action === 'steer') {
      resetMessageHistory()
      onSteer()
      return
    }
    if (action === 'submit-steer') {
      if (!submissionBlocked) {
        resetMessageHistory()
        onSubmitSteer(e)
      }
      return
    }
    if (action === 'submit') {
      if (!submissionBlocked) {
        resetMessageHistory()
        onSubmit(e)
      }
    }
  }

  function handleSubmit(e) {
    resetMessageHistory()
    onSubmit(e)
  }

  const hasFiles = !!pendingFiles?.length

  return (
    <form className="chat__form" onSubmit={handleSubmit}>
      <input
        type="file"
        multiple
        ref={fileInputRef}
        onChange={handleFileSelect}
        onCancel={restoreFocusAfterFilePicker}
        style={{ display: 'none' }}
      />
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
      <div className="chat__input-row">
        {leftButtons}
        <div className={`chat__pill${hasFiles ? ' chat__pill--with-attach' : ''}`}>
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
              onFocus={() => setSlashInputFocused(true)}
              onBlur={() => setSlashInputFocused(false)}
              placeholder="Message Möbius…"
              aria-label="Message Möbius…"
              name="message"
              autoComplete="off"
              rows={1}
              // Combobox semantics apply only while the menu is open. Left on
              // permanently they would announce this plain prose textarea as a
              // picker in every ordinary message the user writes.
              {...(slashMatches.length > 0 ? {
                role: 'combobox',
                'aria-expanded': true,
                'aria-controls': slashListId,
                'aria-activedescendant': slashOptionId,
                'aria-autocomplete': 'list',
              } : {})}
            />
            {rightButtons}
            <PrimaryAction
              sending={sending}
              listening={listening}
              hasInput={hasInput}
              hasUploading={hasUploading}
              offline={offline}
              showSteer={showSteer}
              steerReady={steerReady}
              submissionBlocked={submissionBlocked}
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
