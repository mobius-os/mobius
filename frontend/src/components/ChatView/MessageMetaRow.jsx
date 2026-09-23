/* MessageMetaRow keeps an owner's message timestamp and copy action in one
   revealable row. */
import MessageCopyButton from './MessageCopyButton.jsx'
import { formatDateTime } from '../../lib/dateTimeFormat.js'


export default function MessageMetaRow({
  timestamp,
  copyText,
  visible,
}) {
  if (!timestamp && !copyText) return null

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
      {copyText && <MessageCopyButton text={copyText} />}
    </div>
  )
}
