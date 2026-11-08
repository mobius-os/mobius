/* MessageMetaRow keeps a message's time and actions in one row: its time,
   Copy, and on the chat's reply footer (`side="start"`) Branch. Beside an
   owner message's bubble it is revealed on tap; the reply footer reads how
   long ago the reply was, aligned with the reply's prose, and on a fine
   pointer appears when the reply is hovered or the row is focused.
   `copy(button)` returns the clipboard payload (see MessageCopyButton);
   `branch()` opens a new chat continuing through this reply. */
import { useEffect, useState } from 'react'
import MessageBranchButton from './MessageBranchButton.jsx'
import MessageCopyButton from './MessageCopyButton.jsx'
import { formatDateTime, formatRecentDateTime } from '../../lib/dateTimeFormat.js'

/** The current time, refreshed each minute while `ticking`, and at once
 *  when a suspended tab is shown again. */
function useMinuteClock(ticking) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!ticking) return undefined
    const refresh = () => setNow(Date.now())
    refresh()
    const timer = setInterval(refresh, 60_000)
    const onVisible = () => { if (document.visibilityState === 'visible') refresh() }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      clearInterval(timer)
      document.removeEventListener('visibilitychange', onVisible)
    }
  }, [ticking])
  return now
}

export default function MessageMetaRow({
  timestamp,
  copy,
  branch,
  side = 'end',
  visible,
}) {
  const footer = side === 'start'
  const now = useMinuteClock(footer && !!timestamp)
  // A reply's start row is a line of its own, so it renders even with nothing
  // to show yet (a streaming reply); the overlay row beside a bubble does not.
  if (!footer && !timestamp && !copy) return null

  const time = timestamp && (footer
    ? <time className="chat__ts" title={formatDateTime(timestamp)}>{formatRecentDateTime(timestamp, now)}</time>
    : <time className="chat__ts">{formatDateTime(timestamp)}</time>)
  // CSS visibility owns hiding: a hidden row leaves the focus order and the
  // accessibility tree, and a revealed one rejoins both.
  return (
    <div
      className={`chat__msg-meta chat__msg-meta--${side}${visible ? ' chat__msg-meta--visible' : ''}`}
    >
      {time}
      {copy && <MessageCopyButton read={copy} />}
      {branch && <MessageBranchButton branch={branch} />}
    </div>
  )
}
