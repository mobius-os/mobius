/* The key-request lines inside a saved question card (see useKeyRequestCard.js), from the card every place shares (KeyRequest.jsx). */
import { KeyAnyway, KeyRequestBody } from '../SettingsView/KeyRequest.jsx'
import { VAULT_DOWN } from '../../lib/keysSettingsText.js'

// What the card says when it has no live request to show. A request no
// longer waiting says what the vault shows instead, and so which answer is
// true (keyRequestCardState.js).
const STATE_LINES = {
  loading: 'Loading the key request…',
  unavailable: VAULT_DOWN,
  error: 'Couldn’t load this key request. Reload to try again.',
  guest: 'Only the owner of this Möbius can answer a key request.',
  denied: 'You said Not now to this request.',
  allowed: 'This request isn’t waiting any more: your agents may already use this key on these websites. Choose Allow to tell your agent.',
  gone: 'This request isn’t waiting any more, and nothing was allowed for it. Choose Not now to tell your agent.',
}

/**
 * What the agent says it needs the key for, and where it goes. The card's
 * own choices already say what each answer does (the server's words), so
 * the shared card's note is left out here.
 */
export default function KeyRequestDetails({ card }) {
  const { request, words } = card
  if (card.state === 'idle') return null
  if (card.state !== 'pending' || !request || !words) {
    return <p className="qcard__key-line" aria-busy={card.state === 'loading' || undefined}>{STATE_LINES[card.state]}</p>
  }
  return (
    <div className="qcard__key">
      <KeyRequestBody request={request} words={words} note={false} />
      {words.allowHidden && <KeyAnyway anyway={words.anyway} onOpen={card.showAllow} />}
    </div>
  )
}

