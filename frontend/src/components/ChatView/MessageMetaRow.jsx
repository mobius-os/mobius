/* MessageMetaRow keeps a message's timestamp and copy action in one
   revealable row. Sits on the same side as its message bubble — flush right
   for the user (own messages), flush left for the assistant — via the
   `role` modifier class. */
import MessageCopyButton from './MessageCopyButton.jsx'


export default function MessageMetaRow({
  timestamp,
  copyText,
  role,
  visible,
}) {
  if (!timestamp && !copyText) return null

  const sideClass = role === 'assistant' ? ' chat__msg-meta--assistant' : ''

  return (
    <div
      className={`chat__msg-meta${sideClass}${visible ? ' chat__msg-meta--visible' : ''}`}
      aria-hidden={!visible}
    >
      {timestamp && (
        <time className="chat__ts">
          {new Date(timestamp).toLocaleString([], {
            month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit',
          })}
        </time>
      )}
      {copyText && <MessageCopyButton text={copyText} />}
    </div>
  )
}
