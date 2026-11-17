/* The composer's send-failure notice, with a way back to the parent when a helper's chat refuses the send. */
import { openInternalLink } from './internalLink.js'
import {
  HELPER_CHAT_SEND_LINK_TEXT,
  HELPER_CHAT_SEND_MESSAGE,
  isHelperChatSendFailure,
  sendFailureMessage,
} from './sendFailure.js'

const [beforeLink, afterLink] = HELPER_CHAT_SEND_MESSAGE.split(HELPER_CHAT_SEND_LINK_TEXT)

function HelperChatSendNotice({ parentChatId, onInternalNav }) {
  const href = `/shell?chat=${encodeURIComponent(parentChatId)}`
  return <>
    {beforeLink}
    <a
      className="chat__offline-note-link"
      href={href}
      onClick={event => openInternalLink(event, href, onInternalNav)}
    >{HELPER_CHAT_SEND_LINK_TEXT}</a>
    {afterLink}
  </>
}

/** What the composer says after a refused send: plain copy, or a linked notice. */
export function sendFailureNotice(error, { online = true, onInternalNav } = {}) {
  if (isHelperChatSendFailure(error) && error.parentChatId) {
    return <HelperChatSendNotice parentChatId={error.parentChatId} onInternalNav={onInternalNav} />
  }
  return sendFailureMessage(error, { online })
}
