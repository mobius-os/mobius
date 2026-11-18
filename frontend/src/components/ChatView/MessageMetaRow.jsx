/* MessageMetaRow keeps a message's time and actions in one row: its time,
   Copy, and on an answer's reply footer (`side="start"`) Branch and any
   References (its children). Beside an owner message's bubble it is revealed
   on tap; a reply footer reads how long ago the answer settled (or, until
   that is known, began), aligned with the reply's prose, and appears when the
   reply is tapped, keyboard-focused, or hovered with a fine pointer.
   `copy(button)` returns the clipboard payload (see MessageCopyButton);
   `branch()` opens a new chat continuing through this reply. */
import MessageBranchButton from './MessageBranchButton.jsx'
import MessageCopyButton from './MessageCopyButton.jsx'
import { formatDateTime, formatRecentDateTime } from '../../lib/dateTimeFormat.js'
import { useMinuteNow } from './countdownClock.js'

export default function MessageMetaRow({
  timestamp,
  timeIsStart = false,
  copy,
  branch,
  side = 'end',
  visible,
  children,
}) {
  const footer = side === 'start'
  // One shared minute clock serves every footer showing a relative time.
  const now = useMinuteNow(footer && !!timestamp)
  // A reply's start row is a line of its own, so it renders even with nothing
  // to show yet (a streaming reply); the overlay row beside a bubble does not.
  if (!footer && !timestamp && !copy) return null

  // A footer time that is only when the reply began says so.
  const recent = timestamp && formatRecentDateTime(timestamp, now)
  const time = timestamp && (footer
    ? <time className="chat__ts" title={`${timeIsStart ? 'Started ' : ''}${formatDateTime(timestamp)}`}>
      {timeIsStart ? `Started ${recent === 'Just now' ? 'just now' : recent}` : recent}
    </time>
    : <time className="chat__ts">{formatDateTime(timestamp)}</time>)
  // CSS owns hiding: a hidden owner row leaves the focus order and the
  // accessibility tree, and a revealed one rejoins both; the reply footer
  // stays reachable by keyboard while transparent.
  return (
    <div
      className={`chat__msg-meta chat__msg-meta--${side}${visible ? ' chat__msg-meta--visible' : ''}`}
    >
      {time}
      {copy && <MessageCopyButton read={copy} />}
      {branch && <MessageBranchButton branch={branch} />}
      {children}
    </div>
  )
}
