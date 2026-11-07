/* The composer's send-failure notice, with a way back to the parent when a helper's chat refuses the send. */
import { isPlainLeftClick } from './internalLink.js'
import { isHelperChatSendFailure, sendFailureMessage } from './sendFailure.js'

function HelperChatSendNotice({ parentChatId, onInternalNav }) {
  const href = `/shell?chat=${encodeURIComponent(parentChatId)}`
  return <>
    This is a helper’s chat. Message it from its{' '}
    <a
      className="chat__offline-note-link"
      href={href}
      onClick={event => {
        if (!onInternalNav || !isPlainLeftClick(event)) return
        event.preventDefault()
        onInternalNav(new URL(href, window.location.origin))
      }}
    >parent chat</a>.
  </>
}

/** What the composer says after a refused send: plain copy, or a linked notice. */
export function sendFailureNotice(error, { online = true, onInternalNav } = {}) {
  if (isHelperChatSendFailure(error) && error.parentChatId) {
    return <HelperChatSendNotice parentChatId={error.parentChatId} onInternalNav={onInternalNav} />
  }
  return sendFailureMessage(error, { online })
}
