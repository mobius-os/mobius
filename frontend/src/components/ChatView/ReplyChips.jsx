/**
 * ReplyChips — the stacked row of pending "reply to a quote" drafts, shown
 * above the composer's input row. Each chip is one ReplySelectionRow +
 * ReplyNoteEditor round trip; all of them get bundled into the next real
 * send (see replyQuotes.formatPendingRepliesForSend) and cleared together.
 */
import { Quote } from '@openai/apps-sdk-ui/components/Icon'
import { truncateForChip } from './replyQuotes.js'

export default function ReplyChips({ replies, onRemove }) {
  if (!replies?.length) return null
  return (
    <div className="chat__reply-tray">
      {replies.map(reply => (
        <div key={reply.id} className="chat__reply-chip" title={reply.quote}>
          <Quote className="chat__reply-chip-icon" width={13} height={13} />
          <span className="chat__reply-chip-text">
            {truncateForChip(reply.note, 60)}
          </span>
          <button
            type="button"
            className="chat__reply-chip-remove"
            onPointerDown={(e) => e.preventDefault()}
            onClick={() => onRemove(reply.id)}
            aria-label="Remove this reply"
          >×</button>
        </div>
      ))}
    </div>
  )
}
