/** ChatUsageStrip opens the chat's completed-turn usage summary. */

import { ChevronRight } from '@openai/apps-sdk-ui/components/Icon'
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
        aria-label={`${text} for this chat. Open usage and reported cost breakdown.`}
      >
        <span>{text}</span>
        <ChevronRight width={14} height={14} aria-hidden="true" />
      </button>
    </div>
  )
}
