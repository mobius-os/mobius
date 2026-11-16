/* The key-request lines inside a saved question card (see useKeyRequestCard.js). */
import { sentAs, siteLine } from '../../lib/keysSettingsText.js'

const STATE_LINES = {
  loading: 'Loading the key request…',
  unavailable: 'The key vault isn’t running. It starts with Möbius; a restart usually brings it back.',
  error: 'Couldn’t load this key request. Reload to try again.',
  guest: 'Only the owner of this Möbius can answer a key request.',
  denied: 'You said Not now to this request.',
  gone: 'This request was already allowed.',
}

/** What the agent says it needs the key for, and where it goes. */
export default function KeyRequestDetails({ card }) {
  const { request, words } = card
  if (card.state === 'idle') return null
  if (card.state !== 'pending' || !request || !words) {
    return <p className="qcard__key-line" aria-busy={card.state === 'loading' || undefined}>{STATE_LINES[card.state]}</p>
  }
  const sites = request.sites || []
  return (
    <div className="qcard__key">
      {request.reason && (
        <p className="qcard__key-reason"><span className="qcard__key-muted">{words.says}</span> {request.reason}</p>
      )}
      {words.warning && <p className="qcard__key-warning" role="note">{words.warning}</p>}
      <p className="qcard__key-muted">{words.note}</p>
      <details className="qcard__key-details">
        <summary>Details</summary>
        <ul>
          {sites.map((site, index) => <li key={`${site.host}-${index}`}>{siteLine(site)}</li>)}
          {card.needsValue && <li>Sent as <code>{sentAs(request.inject || { type: 'header', name: 'Authorization', prefix: 'Bearer ' })}</code></li>}
          <li>Saved as “{request.key}”</li>
        </ul>
      </details>
    </div>
  )
}

/** Once the owner chooses Allow for a key they have not saved yet: where to paste it. */
export function KeyPasteField({ card, disabled }) {
  if (card.state !== 'pending' || !card.needsValue || !card.words) return null
  const { words } = card
  return (
    <label className="qcard__key-paste">
      <span>{words.pasteTitle}</span>
      <span className="qcard__composer">
        <input
          className="qcard__input"
          type="password"
          value={card.value}
          onChange={event => card.setValue(event.target.value)}
          placeholder="Paste the key"
          autoComplete="new-password"
          spellCheck={false}
          disabled={disabled}
        />
      </span>
      <small className="qcard__key-muted">{words.pasteNote}</small>
    </label>
  )
}
