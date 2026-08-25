/**
 * ReplySelectionRow — the "Reply to selection" affordance shown above the
 * composer's input row while an assistant-message selection is live (see
 * assistantSelection.js). Tapping it opens ReplyNoteEditor with the selected
 * text; the composer is the stable, expected home for this rather than a
 * control floating over the transcript (see assistantSelection.js for why).
 */
import { Quote } from '@openai/apps-sdk-ui/components/Icon'
import { truncateForChip } from './replyQuotes.js'

export default function ReplySelectionRow({ selectedText, onReply }) {
  if (!selectedText) return null
  return (
    <button
      type="button"
      className="chat__reply-select-row"
      // Keep the selection alive through the tap — a focus shift before
      // click fires would collapse it on most browsers, the same trick the
      // composer already uses for its remove/× controls.
      onPointerDown={(e) => e.preventDefault()}
      onClick={() => onReply(selectedText)}
    >
      <Quote className="chat__reply-select-row-icon" width={14} height={14} />
      <span className="chat__reply-select-row-label">Reply to “{truncateForChip(selectedText, 40)}”</span>
    </button>
  )
}
