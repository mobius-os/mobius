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
 *       <AttachButton ... />,
 *       <SlashPicker ... />,  // future: model / skill / thinking
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
 * ║      `handleTextareaChange` toggles `chat__pill--tall` when      ║
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
 * ║                                                                  ║
 * ║   3. CHIP × BUTTON keeps the keyboard                            ║
 * ║      The remove-attachment × has `onPointerDown.preventDefault`  ║
 * ║      just like every other interactive composer element.         ║
 * ║      Without it, tapping × steals focus → iOS collapses kb.      ║
 * ║                                                                  ║
 * ║   4. ICONS COME FROM THE APPS-SDK-UI PACKAGE                     ║
 * ║      Primary action: `ArrowUp` (22) for send, `Mic` (24) for     ║
 * ║      voice, inlined stop-square SVG for stop. The package        ║
 * ║      ships these — don't substitute hand-rolled paths.           ║
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
 * ║   7. `_isTouchPrimary` is detected once via                      ║
 * ║      `matchMedia('(hover: none) and (pointer: coarse)')` and     ║
 * ║      gates Enter-to-send. Touch devices: Enter inserts a         ║
 * ║      newline. Desktop: Enter sends.                              ║
 * ║                                                                  ║
 * ╚══════════════════════════════════════════════════════════════════╝
 */

import { useRef, useLayoutEffect } from 'react'
import { ArrowUp, Mic } from '@openai/apps-sdk-ui/components/Icon'


// Detect touch-primary once (same heuristic ChatView uses).
const _touchMql = typeof matchMedia === 'function'
  ? matchMedia('(hover: none) and (pointer: coarse)')
  : null
let _isTouchPrimary = _touchMql?.matches ?? false
_touchMql?.addEventListener('change', (e) => { _isTouchPrimary = e.matches })


/** The primary action button — Send / Stop / Mic — auto-resolved
 *  from the bar's input/sending/listening/uploading state. */
function PrimaryAction({
  sending, listening, hasInput, hasUploading,
  onSubmit, onStop, onToggleVoice,
}) {
  if (sending && !hasInput) {
    return (
      <button className="chat__stop" type="button" onClick={onStop} aria-label="Stop">
        <svg width="16" height="16" viewBox="0 0 12 12" fill="currentColor">
          <rect width="12" height="12" rx="2" />
        </svg>
      </button>
    )
  }
  if (hasInput && !listening) {
    return (
      <button
        className="chat__send"
        type="button"
        onTouchEnd={(e) => { e.preventDefault(); onSubmit(e) }}
        onClick={onSubmit}
        aria-label="Send"
        disabled={hasUploading}
      >
        <ArrowUp width={22} height={22} />
      </button>
    )
  }
  return (
    <button
      className={`chat__mic ${listening ? 'chat__mic--active' : ''}`}
      type="button"
      onTouchEnd={(e) => { e.preventDefault(); onToggleVoice() }}
      onClick={onToggleVoice}
      aria-label={listening ? 'Stop recording' : 'Voice input'}
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
function FileChips({ files, onRemove }) {
  if (!files?.length) return null
  return (
    <div className="chat__attach-tray">
      {files.map(chip => {
        const isImage = !!chip.objectUrl
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
            title={chip.status === 'error' ? chip.error : chip.name}
          >
            {isImage ? (
              <img className="chat__attach-card-thumb" src={chip.objectUrl} alt="" />
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
 *   onSubmit           — called with FormEvent | MouseEvent | TouchEvent
 *   inputRef           — for caller to focus/blur (e.g. dismiss keyboard)
 *   sending            — agent is currently streaming
 *   listening          — voice input active
 *   listeningRef       — synchronous mirror (for guarding textarea onChange)
 *   onToggleVoice      — mic button handler
 *   onStop             — stop button handler
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
 *
 * The bar does NOT own send state — ChatView's doSend handles that.
 * The bar's only job: composition + the Send/Stop/Mic resolution.
 */
export default function ChatInputBar({
  input,
  onInputChange,
  onSubmit,
  inputRef,
  sending,
  listening,
  listeningRef,
  onToggleVoice,
  onStop,
  pendingFiles,
  onAddFiles,
  onRemoveFile,
  leftButtons,
  rightButtons,
  attachTriggerRef,
}) {
  const fileInputRef = useRef(null)
  // Captures whether the textarea was focused at the moment the file
  // picker opened. Read by `handleFileSelect` to decide whether to
  // refocus the textarea after the picker closes — refocusing
  // unconditionally would pop the soft keyboard up even when the
  // keyboard was down before the `+` tap.
  const wasInputFocusedAtPickerOpenRef = useRef(false)

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

  const hasInput = !!input.trim()
  const hasUploading = pendingFiles?.some(c => c.status === 'uploading') ?? false

  function handleFileSelect(e) {
    const fileList = Array.from(e.target.files || [])
    if (!fileList.length) return
    e.target.value = ''
    onAddFiles(fileList)
    // Only refocus the textarea (reopening the soft keyboard) if it
    // was focused BEFORE the OS file picker opened. Unconditional
    // refocus would pop the keyboard up even when the user tapped
    // `+` with the keyboard down — see the matching contract in
    // ComposerPopover and ChatSettingsPanel.
    if (wasInputFocusedAtPickerOpenRef.current) {
      setTimeout(() => inputRef?.current?.focus({ preventScroll: true }), 0)
    }
  }

  function handleTextareaChange(e) {
    if (listeningRef?.current) return  // voice in progress
    onInputChange(e.target.value)
    e.target.style.height = 'auto'
    const h = Math.min(e.target.scrollHeight, 280)
    e.target.style.height = h + 'px'
    // Toggle the `--tall` class only when the textarea ACTUALLY
    // spans multiple lines. A single line of 16px text at line-
    // height 1.45 with 8px padding measures ~31px scrollHeight,
    // so a threshold of 30 was triggering --tall on every keystroke
    // and dropping the cursor + mic to the bottom. 45px sits
    // safely between single-line (~31) and two-line (~55).
    const pill = e.target.closest('.chat__pill')
    if (pill) pill.classList.toggle('chat__pill--tall', h > 45)
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey && !_isTouchPrimary) {
      e.preventDefault()
      onSubmit(e)
    }
  }

  const hasFiles = !!pendingFiles?.length

  return (
    <form className="chat__form" onSubmit={onSubmit}>
      <input
        type="file"
        multiple
        ref={fileInputRef}
        onChange={handleFileSelect}
        style={{ display: 'none' }}
      />
      <div className="chat__input-row">
        {leftButtons}
        <div className={`chat__pill${hasFiles ? ' chat__pill--with-attach' : ''}`}>
          {hasFiles && (
            <FileChips files={pendingFiles} onRemove={onRemoveFile} />
          )}
          <div className="chat__input-line">
            <textarea
              ref={inputRef}
              className="chat__input"
              value={input}
              onChange={handleTextareaChange}
              onKeyDown={handleKeyDown}
              placeholder="Message the agent..."
              rows={1}
            />
            {rightButtons}
            <PrimaryAction
              sending={sending}
              listening={listening}
              hasInput={hasInput}
              hasUploading={hasUploading}
              onSubmit={onSubmit}
              onStop={onStop}
              onToggleVoice={onToggleVoice}
            />
          </div>
        </div>
      </div>
    </form>
  )
}
