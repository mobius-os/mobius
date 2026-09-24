/* MessageMetaRow reveals an owner message timestamp without adding copy controls. */
import { formatDateTime } from '../../lib/dateTimeFormat.js'

export default function MessageMetaRow({
  timestamp,
  visible,
}) {
  if (!timestamp) return null

  return (
    <div
      className={`chat__msg-meta${visible ? ' chat__msg-meta--visible' : ''}`}
      aria-hidden={!visible}
    >
      {timestamp && (
        <time className="chat__ts">
          {formatDateTime(timestamp)}
        </time>
      )}
    </div>
  )
}
