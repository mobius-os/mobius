/* MessageMetaRow keeps a settled message's timestamp and copy action in one
   revealable row. */
import MessageCopyButton from './MessageCopyButton.jsx'
import MessageSpeakButton from './MessageSpeakButton.jsx'


export default function MessageMetaRow({
  timestamp,
  copyText,
  speechText,
  speechKey,
  speechChatId,
  visible,
}) {
  if (!timestamp && !copyText && !speechText) return null

  return (
    <div
      className={`chat__msg-meta${visible ? ' chat__msg-meta--visible' : ''}`}
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
      {speechText && (
        <MessageSpeakButton
          chatId={speechChatId}
          messageKey={speechKey}
          text={speechText}
        />
      )}
    </div>
  )
}
