/* One app's or agent's key request card, as Settings › Keys, a chat and an app's window show it. */
import { useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { api, jsonOrThrow } from '../../api/client.js'
import { requestWording, sentAs, siteLine } from '../../lib/keysSettingsText.js'
import './KeysSection.css'

export const BEARER = { type: 'header', name: 'Authorization', prefix: 'Bearer ' }

function Lead({ segments }) {
  return segments.map((segment, index) => (
    typeof segment === 'string'
      ? <span key={index}>{segment}</span>
      : <strong key={index}>{segment.text}</strong>
  ))
}

/**
 * Not now / Allow answer the request through the owner-only routes with its
 * ticket; a pasted value goes only to the approve route, never anywhere else.
 * ``onAnswered`` hears ``'approve'`` or ``'deny'`` once the vault accepted it.
 * ``readOnly`` shows what is asked without the paste field or answers, for a
 * card the owner answers elsewhere.
 */
export default function KeyRequest({ request, names, onAnswered, readOnly = false }) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const words = requestWording(request, names)
  // The requester only proposes: Allow grants exactly what this card shows.
  // An app's sites go to that app's own grant; an agent's to the sites for
  // the owner and agents.
  const inject = request.inject || BEARER
  const sites = request.sites || []

  async function answer(kind) {
    setBusy(kind)
    setError('')
    try {
      const response = kind === 'approve'
        ? await api.keys.approve(request.id, {
          ticket: request.ticket, sites, ...(words.needsValue ? { value, inject } : {}),
        })
        : await api.keys.deny(request.id, request.ticket)
      await jsonOrThrow(response, kind === 'approve' ? 'Allow' : 'Not now')
      setValue('')
      onAnswered(kind)
    } catch (err) {
      setError(err.message)
      setBusy('')
    }
  }

  const denyButton = (
    <button
      type="button"
      className={`settings__btn settings__btn--sm${words.allowHidden ? '' : ' settings__btn--outline'}`}
      onClick={() => answer('deny')}
      disabled={Boolean(busy)}
    >
      Not now
    </button>
  )
  const allowButton = (label, primary) => (
    <button
      type="button"
      className={`settings__btn settings__btn--sm${primary ? '' : ' settings__btn--outline'}`}
      onClick={() => answer('approve')}
      disabled={Boolean(busy) || (words.needsValue && !value)}
    >
      {busy === 'approve' ? 'Allowing…' : label}
    </button>
  )

  return (
    <div className="settings-keys__request" role="group" aria-label={`${words.who} asks to use ${request.key}`}>
      <p className="settings-keys__request-lead"><Lead segments={words.lead} /></p>
      {request.reason && <p className="settings-keys__muted settings-keys__reason">“{request.reason}”</p>}
      {words.warning && <Alert color="warning" variant="soft" description={words.warning} />}
      <p className="settings-keys__muted">{words.note}</p>
      {words.needsValue && !readOnly && (
        <input
          className="settings-keys__input"
          type="password"
          value={value}
          onChange={e => setValue(e.target.value)}
          placeholder="Paste the key"
          aria-label="Key value"
          autoComplete="new-password"
          spellCheck={false}
        />
      )}
      <details className="settings-keys__details">
        <summary>Details</summary>
        <ul className="settings-keys__details-list">
          {sites.map((site, index) => <li key={`${site.host}-${index}`}>{siteLine(site)}</li>)}
          {words.needsValue && <li>Sent as <code>{sentAs(inject)}</code></li>}
          <li>Saved as “{request.key}”</li>
        </ul>
      </details>
      {words.allowHidden && !readOnly && (
        <details className="settings-keys__details">
          <summary>Use {words.savedBy}'s key anyway</summary>
          <div className="settings-keys__details-body">
            <p className="settings-keys__muted">
              {words.who}'s requests would be signed with {words.savedBy}'s account, and {words.savedBy} can change that key at any time.
            </p>
            <div className="settings-keys__actions settings-keys__actions--start">
              {allowButton(`Use ${words.savedBy}'s key`, false)}
            </div>
          </div>
        </details>
      )}
      {error && <Alert color="danger" variant="soft" description={error} />}
      {!readOnly && (
        <div className="settings-keys__actions">
          {denyButton}
          {!words.allowHidden && allowButton('Allow', true)}
        </div>
      )}
    </div>
  )
}
