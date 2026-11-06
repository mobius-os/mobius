/* MessageMetaRow keeps a message's time and actions in one revealable row:
   beside an owner message's bubble, or beneath a reply (`side="start"`),
   with Copy immediately after its time, aligned with the reply's prose.
   `copy(button)` returns the clipboard payload (see MessageCopyButton). */
import MessageCopyButton from './MessageCopyButton.jsx'
import { formatDateTime } from '../../lib/dateTimeFormat.js'


export default function MessageMetaRow({
  timestamp,
  copy,
  side = 'end',
  visible,
}) {
  // A reply's start row is a line of its own, so it renders even with nothing
  // to show yet (a streaming reply); the overlay row beside a bubble does not.
  if (side !== 'start' && !timestamp && !copy) return null

  // CSS visibility owns hiding: a hidden row leaves the focus order and the
  // accessibility tree, and a revealed one rejoins both.
  return (
    <div
      className={`chat__msg-meta chat__msg-meta--${side}${visible ? ' chat__msg-meta--visible' : ''}`}
    >
      {timestamp && (
        <time className="chat__ts">
          {formatDateTime(timestamp)}
        </time>
      )}
      {copy && <MessageCopyButton read={copy} />}
    </div>
  )
}
