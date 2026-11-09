/* ComposerHistoryStrip — says which message the chat box holds while browsing
 * earlier messages with ↑/↓, what Enter will do, and that the draft is kept.
 * Its × is the touch and pointer equivalent of Esc. A queued message being
 * edited needs no strip: the queue tray marks its row and offers Stop editing,
 * so the strip appears for it only to warn (a save that failed). */
import { X } from '@openai/apps-sdk-ui/components/Icon'

const DRAFT_PREVIEW_LENGTH = 34

/** The set-aside draft in one short line. Its attached files stay with it
 * (an earlier message sent again goes without them), so they are counted. */
export function composerDraftPreview(draft, fileCount = 0) {
  const flat = String(draft || '').replace(/\s+/g, ' ').trim()
  const text = flat.length > DRAFT_PREVIEW_LENGTH
    ? `${flat.slice(0, DRAFT_PREVIEW_LENGTH - 1)}…`
    : flat
  const files = fileCount > 0 ? `${fileCount} ${fileCount === 1 ? 'file' : 'files'}` : ''
  return [text, files].filter(Boolean).join(' · ')
}

/** The strip's title and hint for one history position. */
export function composerHistoryStripCopy(position, { touch = false, saving = false, note = null } = {}) {
  if (!position) return null
  if (position.kind === 'started') {
    return {
      title: 'This message already started.',
      hint: 'Your text is kept, so you can send it as a new message.',
      warn: true,
    }
  }
  if (position.kind === 'queued') {
    if (!note) return null
    return {
      title: `Editing queued message ${position.queuedNumber} of ${position.queuedCount}`,
      hint: note || (saving
        ? 'Saving…'
        : (touch ? 'Send saves it' : 'Enter saves it, Esc goes back')),
      warn: !!note,
    }
  }
  return {
    title: 'Earlier message',
    hint: touch ? 'Send sends it again' : 'Enter sends it again, Esc goes back',
    warn: false,
  }
}

/** The chat box's screen-reader description of the history keys: the strip
 * only appears once browsing has started, so this is how ↑ is discovered.
 * Null without a keyboard-first input or anything to browse. */
export function composerHistoryKeyHint({ browsing = false, hasEntries = false, touch = false } = {}) {
  if (touch) return null
  if (browsing) return 'Up and Down arrows move between messages. Escape returns to your draft.'
  if (hasEntries) return 'Up arrow opens your queued and earlier messages.'
  return null
}

export default function ComposerHistoryStrip({
  position, note, saving, touch, draftFileCount = 0, onBack,
}) {
  const copy = composerHistoryStripCopy(position, { touch, saving, note })
  if (!copy) return null
  const draft = composerDraftPreview(position.draft, draftFileCount)
  return (
    <div
      className={`chat__history-strip${copy.warn ? ' chat__history-strip--warn' : ''}`}
      role="status"
      aria-live="polite"
    >
      <span className="chat__history-strip-text">
        <b className="chat__history-strip-title">{copy.title}</b>
        {' '}
        <span className="chat__history-strip-hint">{copy.hint}</span>
      </span>
      {draft && (
        <span className="chat__history-strip-draft" title={position.draft}>
          Draft kept: {draft}
        </span>
      )}
      <button
        type="button"
        className="chat__history-strip-back"
        // Keep the composer focused so the soft keyboard stays up.
        onPointerDown={(event) => event.preventDefault()}
        onClick={onBack}
        aria-label="Back to your draft"
        title="Back to your draft (Esc)"
      >
        <X width={14} height={14} aria-hidden="true" />
      </button>
    </div>
  )
}
