/* One app's or agent's key request card, as Settings › Keys, a chat and an app's window show it. */
import { useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { api, jsonOrThrow } from '../../api/client.js'
import useKeyAnswerGuard from './useKeyAnswerGuard.js'
import { requestWording, sentAs, siteLine } from '../../lib/keysSettingsText.js'
import './settingsButtons.css'
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
 * One question at a time. The card asks whether to allow the request; a key
 * the owner has no value for yet takes a second step, after Continue, that
 * asks only for the value (Back returns). Not now / Allow (Save and allow)
 * answer through the owner-only routes with the request's ticket; a pasted
 * value goes only to the approve route, never anywhere else.
 * ``onAnswered`` hears ``'approve'`` or ``'deny'`` once the vault accepted it.
 * ``readOnly`` shows what is asked without the paste field or answers, for a
 * card the owner answers elsewhere.
 */
export default function KeyRequest({ request, names, onAnswered, readOnly = false }) {
  const [value, setValue] = useState('')
  const [pasting, setPasting] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const words = requestWording(request, names)
  // The requester only proposes: Allow grants exactly what this card shows.
  // An app's sites go to that app's own grant; an agent's to the sites for
  // the owner and agents.
  const inject = request.inject || BEARER
  const sites = request.sites || []
  const guard = useKeyAnswerGuard({ enabled: !readOnly })

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
      setPasting(false)
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
      aria-disabled={guard.active ? undefined : 'true'}
      onClick={event => { if (guard.counts(event)) answer('deny') }}
      disabled={Boolean(busy)}
    >
      Not now
    </button>
  )
  const allowButton = (label, primary) => (
    <button
      type="button"
      className={`settings__btn settings__btn--sm${primary ? '' : ' settings__btn--outline'}`}
      aria-disabled={guard.active ? undefined : 'true'}
      onClick={event => { if (guard.counts(event)) answer('approve') }}
      disabled={Boolean(busy) || (words.needsValue && !value)}
    >
      {busy === 'approve' ? 'Allowing…' : label}
    </button>
  )

  // Continue answers nothing, so it needs no guard; the second step's new
  // size wakes the guard again, so a double tap never lands on Save and allow.
  const twoSteps = words.needsValue && !readOnly
  if (twoSteps && pasting) {
    return (
      <div
        ref={guard.cardRef}
        className="settings-keys__request"
        role="group"
        aria-label={words.pasteTitle}
        onPointerDownCapture={guard.onPressCapture}
      >
        <p className="settings-keys__request-lead"><strong>{words.pasteTitle}</strong></p>
        <input
          className="settings-keys__input"
          type="password"
          value={value}
          onChange={e => setValue(e.target.value)}
          placeholder="Paste the key"
          aria-label="Key value"
          autoComplete="new-password"
          spellCheck={false}
          autoFocus
        />
        <p className="settings-keys__muted">{words.pasteNote}</p>
        {error && <Alert color="danger" variant="soft" description={error} />}
        <div className="settings-keys__actions">
          <button
            type="button"
            className="settings__btn settings__btn--sm settings__btn--outline"
            onClick={() => { setPasting(false); setError('') }}
            disabled={Boolean(busy)}
          >
            Back
          </button>
          {allowButton('Save and allow', true)}
        </div>
      </div>
    )
  }

  return (
    <div
      ref={guard.cardRef}
      className="settings-keys__request"
      role="group"
      aria-label={`${words.who} asks to use ${request.key}`}
      onPointerDownCapture={guard.onPressCapture}
    >
      <p className="settings-keys__request-lead"><Lead segments={words.lead} /></p>
      {request.reason && (
        <p className="settings-keys__says">
          <span className="settings-keys__muted">{words.says}</span> {request.reason}
        </p>
      )}
      {words.warning && <Alert color="warning" variant="soft" description={words.warning} />}
      <p className="settings-keys__muted">{words.note}</p>
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
          {twoSteps
            ? (
              <button type="button" className="settings__btn settings__btn--sm" onClick={() => setPasting(true)} disabled={Boolean(busy)}>
                Continue
              </button>
            )
            : !words.allowHidden && allowButton('Allow', true)}
        </div>
      )}
    </div>
  )
}
