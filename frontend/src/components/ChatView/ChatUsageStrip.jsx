/**
 * ChatUsageStrip — the always-visible, subtle cost/token readout that sits
 * directly above the composer (between the last transcript chrome and
 * ChatInputBar in ChatView.jsx). Moved here from an overlay badge on the
 * brain icon per owner feedback: that badge was easy to miss and had no
 * room for more than one number. This strip has room for cost AND the
 * input/output split, and sits somewhere the eye already lands right above
 * where a turn's cost just changed.
 *
 * "Live" here means "updates the moment a turn's usage is durable" — the
 * chat-usage query is invalidated on every stream 'done' in ChatView, so
 * this repaints within a network round-trip of a turn finishing. It is NOT
 * a token-by-token ticker: Claude's SDK only reports usage on the terminal
 * result of a turn (no incremental figure exists mid-turn to show), so a
 * running turn correctly shows the PRE-turn total until it completes rather
 * than a fake live number.
 *
 * Clicking opens the same full breakdown as the popover's "Token usage &
 * cost" row — one destination, two entry points.
 */

import { formatUsageStripText } from './chatUsageFormat.js'

export default function ChatUsageStrip({ totals, onOpen }) {
  const text = formatUsageStripText(totals)
  if (!text) return null
  return (
    <div className="chat__usage-strip-wrap">
      <button
        type="button"
        className="chat__usage-strip"
        onClick={onOpen}
        aria-label={`${text} for this chat. Open token usage and cost breakdown.`}
      >
        {text}
      </button>
    </div>
  )
}
