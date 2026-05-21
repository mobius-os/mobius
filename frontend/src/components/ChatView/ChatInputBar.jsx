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
 */

import { useRef } from 'react'


// Detect touch-primary once (same heuristic ChatView uses).
const _touchMql = typeof matchMedia === 'function'
  ? matchMedia('(hover: none) and (pointer: coarse)')
  : null
let _isTouchPrimary = _touchMql?.matches ?? false
_touchMql?.addEventListener('change', (e) => { _isTouchPrimary = e.matches })


/** Default attach-files button. Exposed as a left-button preset
 *  callers can include or omit. Future "/" picker button would
 *  follow the same pattern (own state, own popover, mount via
 *  leftButtons slot). */
function AttachButton({ onClick }) {
  return (
    <button
      type="button"
      className="chat__attach"
      onClick={onClick}
      aria-label="Attach files"
    >
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/>
      </svg>
    </button>
  )
}


/** The primary action button — Send / Stop / Mic — auto-resolved
 *  from the bar's input/sending/listening/uploading state. */
function PrimaryAction({
  sending, listening, hasInput, hasUploading,
  onSubmit, onStop, onToggleVoice,
}) {
  if (sending && !hasInput) {
    return (
      <button className="chat__stop" type="button" onClick={onStop} aria-label="Stop">
        <svg width="12" height="12" viewBox="0 0 12 12" fill="currentColor">
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
        <svg width="13" height="13" viewBox="0 0 13 13" fill="none">
          <path d="M6.5 11V2M2 6.5l4.5-4.5 4.5 4.5" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
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
      <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
        <rect x="4.5" y="1" width="5" height="8" rx="2.5" stroke="currentColor" strokeWidth="1.3"/>
        <path d="M3 7a4 4 0 008 0" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
        <path d="M7 11v2" stroke="currentColor" strokeWidth="1.3" strokeLinecap="round"/>
      </svg>
    </button>
  )
}


/** File-upload chips (rendered above the input row when files exist). */
function FileChips({ files, onRemove }) {
  if (!files?.length) return null
  return (
    <div className="chat__chips">
      {files.map(chip => (
        <div
          key={chip.id}
          className={`chat__chip${chip.status === 'error' ? ' chat__chip--error' : ''}${chip.objectUrl ? ' chat__chip--image' : ''}`}
          title={chip.status === 'error' ? chip.error : chip.name}
        >
          {chip.objectUrl && (
            <img className="chat__chip-thumb" src={chip.objectUrl} alt="" />
          )}
          <span className="chat__chip-name">{chip.name}</span>
          <span className="chat__chip-status">
            {chip.status === 'uploading' ? 'uploading…'
              : chip.status === 'error' ? 'error'
              : `${Math.round(chip.size / 1024)}KB`}
          </span>
          <button
            type="button"
            className="chat__chip-remove"
            onClick={() => onRemove(chip.id)}
            aria-label={`Remove ${chip.name}`}
          >×</button>
        </div>
      ))}
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
 *   leftButtons        — extra buttons after the default attach button
 *                        (e.g., future <SlashPicker />)
 *   rightButtons       — extra buttons before the primary action
 *                        (reserved for future use)
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
}) {
  const fileInputRef = useRef(null)

  const hasInput = !!input.trim()
  const hasUploading = pendingFiles?.some(c => c.status === 'uploading') ?? false

  function handleFileSelect(e) {
    const fileList = Array.from(e.target.files || [])
    if (!fileList.length) return
    e.target.value = ''
    onAddFiles(fileList)
  }

  function handleTextareaChange(e) {
    if (listeningRef?.current) return  // voice in progress
    onInputChange(e.target.value)
    e.target.style.height = 'auto'
    e.target.style.height = Math.min(e.target.scrollHeight, 160) + 'px'
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey && !_isTouchPrimary) {
      e.preventDefault()
      onSubmit(e)
    }
  }

  return (
    <form className="chat__form" onSubmit={onSubmit}>
      <FileChips files={pendingFiles} onRemove={onRemoveFile} />
      <div className="chat__input-row">
        <input
          type="file"
          multiple
          ref={fileInputRef}
          onChange={handleFileSelect}
          style={{ display: 'none' }}
        />
        <AttachButton onClick={() => fileInputRef.current?.click()} />
        {leftButtons}
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
    </form>
  )
}
