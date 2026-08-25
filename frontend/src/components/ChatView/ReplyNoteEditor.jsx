/**
 * ReplyNoteEditor — the small text window opened by ReplySelectionRow's
 * "Reply to selection" button. Shows the quoted excerpt and lets the owner
 * type what they want to address about it. Saving (button or Enter) stacks it as a
 * pending reply chip above the composer — it never sends anything itself.
 */
import { useRef, useState } from 'react'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { truncateForChip } from './replyQuotes.js'

export default function ReplyNoteEditor({ quote, onSave, onClose }) {
  const [note, setNote] = useState('')
  const dialogRef = useRef(null)
  const textareaRef = useRef(null)

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: textareaRef,
    onClose,
  })

  function commit() {
    const trimmed = note.trim()
    if (!trimmed) return
    onSave(trimmed)
  }

  function handleKeyDown(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      commit()
    }
  }

  return (
    <div className="chat__reply-editor-overlay" role="presentation" onClick={onClose}>
      <div
        ref={dialogRef}
        className="chat__reply-editor"
        role="dialog"
        aria-modal="true"
        aria-labelledby="reply-editor-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="chat__reply-editor-head">
          <h2 id="reply-editor-title" className="chat__reply-editor-title">Reply to this</h2>
          <button
            type="button"
            className="chat__reply-editor-close"
            onClick={onClose}
            aria-label="Cancel"
          >×</button>
        </div>
        <blockquote className="chat__reply-editor-quote">
          {truncateForChip(quote, 220)}
        </blockquote>
        <textarea
          ref={textareaRef}
          className="chat__reply-editor-input"
          value={note}
          onChange={(e) => setNote(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="What do you want to address?"
          aria-label="Your note about this excerpt"
          rows={3}
        />
        <div className="chat__reply-editor-foot">
          <button
            type="button"
            className="chat__reply-editor-btn chat__reply-editor-btn--ghost"
            onClick={onClose}
          >
            Cancel
          </button>
          <button
            type="button"
            className="chat__reply-editor-btn"
            onClick={commit}
            disabled={!note.trim()}
          >
            Save
          </button>
        </div>
      </div>
    </div>
  )
}
