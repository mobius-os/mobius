/* One app's or agent's key request card, and the parts every place that asks one shares: Settings › Keys, the dialog over an app, and an agent's saved card in a chat (KeyQuestionCard.jsx). */
import { useId, useRef, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { api, jsonOrThrow } from '../../api/client.js'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import useKeyAnswerGuard, { earlyAnswerHint } from './useKeyAnswerGuard.js'
import {
  approvalBody, newKeyParts, partLabel, partsFilled, plainPartsNote, requestWording, sentAs, siteLine, tokenClaims,
} from '../../lib/keysSettingsText.js'
import { keyFileDropProps } from '../../lib/keyFileDrop.js'
import './settingsButtons.css'
import './KeysSection.css'

/**
 * Answering one request, wherever its card is: the words it asks with
 * (``words``), the answer guard every card wears (``guard``), the value
 * pasted for a key the owner has no value for yet, and ``send(kind)``, which
 * answers the vault through the owner-only routes with the request's ticket:
 * ``'allow'`` grants exactly what the card shows, ``'deny'`` refuses it. A
 * pasted value goes only to the approve route, never anywhere else; it throws
 * when the vault refuses. A new key of several parts (``parts``) is pasted
 * one field per part (``partValues``); ``filled`` says every field it needs
 * has text. ``request`` is null while there is nothing to answer, and the
 * guard then sleeps.
 */
export function useKeyAnswer(request, names) {
  const [value, setValue] = useState('')
  const [partValues, setPartValues] = useState({})
  const guard = useKeyAnswerGuard({ enabled: Boolean(request) })
  const parts = request ? newKeyParts(request) : null
  async function send(kind) {
    // Only this request's parts: a request replaced under the same id must
    // never carry values typed for the one before.
    const pasted = parts && Object.fromEntries(parts.map(part => [part.name, partValues[part.name] || '']))
    const response = kind === 'allow'
      ? await api.keys.approve(request.id, approvalBody(request, parts ? pasted : value))
      : await api.keys.deny(request.id, request.ticket)
    await jsonOrThrow(response, kind === 'allow' ? 'Allow' : 'Not now')
    setValue('')
    setPartValues({})
  }
  return {
    words: request ? requestWording(request, names) : null, guard, value, setValue, send,
    parts,
    partValues,
    setPartValue: (name, text) => setPartValues(current => ({ ...current, [name]: text })),
    filled: parts ? partsFilled(parts, partValues) : Boolean(value),
  }
}

/**
 * What a request asks, under its question: the requester's own reason, a
 * warning, where the key goes (``note``, unless the card says it in its own
 * words) and, one tap away, the technical details.
 */
export function KeyRequestBody({ request, words, note = true }) {
  const sites = request.sites || []
  return (
    <>
      {request.reason && (
        <p className="settings-keys__says">
          <span className="settings-keys__muted">{words.says}</span> {request.reason}
        </p>
      )}
      {words.warning && <Alert color="warning" variant="soft" description={words.warning} />}
      {note && <p className="settings-keys__muted">{words.note}</p>}
      <details className="settings-keys__details">
        <summary>Details</summary>
        <ul className="settings-keys__details-list">
          {sites.map((site, index) => <li key={`${site.host}-${index}`}>{siteLine(site)}</li>)}
          {words.slot && <li>Sent as <code>{sentAs(words.slot, request.parts)}</code></li>}
          {tokenClaims(words.slot) && <li>Token: {tokenClaims(words.slot)}</li>}
          {newKeyParts(request) && <li>In {request.parts.length} parts: {request.parts.map(partLabel).join(', ')}</li>}
          {words.keptSlot && (
            <li>
              Sent as <code>{sentAs(words.keptSlot.kept)}</code>, not <code>{sentAs(words.keptSlot.asked, request.parts)}</code> as
              asked: the key keeps how it is sent
            </li>
          )}
          <li>Saved as “{request.key}”</li>
        </ul>
      </details>
    </>
  )
}

/** Allowing another app's key, kept one deliberate step away (``words.anyway``). */
export function KeyAnyway({ anyway, onOpen, children }) {
  return (
    <details className="settings-keys__details" onToggle={event => { if (event.currentTarget.open) onOpen?.() }}>
      <summary>{anyway.label}</summary>
      <div className="settings-keys__details-body">
        <p className="settings-keys__muted">{anyway.note}</p>
        {children}
      </div>
    </details>
  )
}

/**
 * Where the owner pastes the value of a key they have not saved yet: one
 * field, or one per part for a key of several parts (a plain part, such as
 * an account id, is typed in the open).
 */
export function KeyPasteField({ answer, disabled, autoFocus = false }) {
  const id = useId()
  const [dropError, setDropError] = useState('')
  if (answer.parts) {
    const plainNote = plainPartsNote(answer.parts)
    return (
      <div className="settings-keys__paste" role="group" aria-labelledby={id} aria-describedby={`${id}-note`}>
        <p className="settings-keys__request-lead" id={id}><strong>{answer.words.pasteTitle}</strong></p>
        {answer.parts.map((part, index) => (
          <label key={part.name} className="settings-keys__part">
            <span className="settings-keys__muted">{partLabel(part)}</span>
            {part.multiline ? (
              <textarea
                className="settings-keys__input settings-keys__input--file"
                value={answer.partValues[part.name] || ''}
                onChange={event => answer.setPartValue(part.name, event.target.value)}
                {...keyFileDropProps(text => { setDropError(''); answer.setPartValue(part.name, text) }, setDropError)}
                placeholder="Paste the whole file, including its first and last lines"
                spellCheck={false}
                autoCapitalize="off"
                rows={5}
                disabled={disabled}
                autoFocus={autoFocus && index === 0}
              />
            ) : (
              <input
                className="settings-keys__input"
                type={part.secret ? 'password' : 'text'}
                value={answer.partValues[part.name] || ''}
                onChange={event => answer.setPartValue(part.name, event.target.value)}
                placeholder={part.secret ? 'Paste it here' : 'Type or paste it'}
                autoComplete={part.secret ? 'new-password' : 'off'}
                spellCheck={false}
                disabled={disabled}
                autoFocus={autoFocus && index === 0}
              />
            )}
          </label>
        ))}
        {dropError && <p className="settings-keys__muted" role="alert">{dropError}</p>}
        {plainNote && <Alert color="warning" variant="soft" description={plainNote} />}
        <p className="settings-keys__muted" id={`${id}-note`}>{answer.words.pasteNote}</p>
      </div>
    )
  }
  return (
    <div className="settings-keys__paste">
      <label className="settings-keys__request-lead" htmlFor={id}><strong>{answer.words.pasteTitle}</strong></label>
      <input
        id={id}
        className="settings-keys__input"
        type="password"
        value={answer.value}
        onChange={event => answer.setValue(event.target.value)}
        placeholder="Paste the key"
        aria-describedby={`${id}-note`}
        autoComplete="new-password"
        spellCheck={false}
        disabled={disabled}
        autoFocus={autoFocus}
      />
      <p className="settings-keys__muted" id={`${id}-note`}>{answer.words.pasteNote}</p>
    </div>
  )
}

function Lead({ segments }) {
  return segments.map((segment, index) => (
    typeof segment === 'string'
      ? <span key={index}>{segment}</span>
      : <strong key={index}>{segment.text}</strong>
  ))
}

/**
 * The card Settings › Keys and the dialog over an app show. One question at
 * a time: it asks whether to allow the request; a key the owner has no value
 * for yet takes a second step, after Continue, that asks only for the value
 * (Back returns). ``onAnswered`` hears ``'allow'`` or ``'deny'`` once the
 * vault accepted it. As the ``dialog`` over an app it is modal: it takes the
 * keyboard from the app's frame, starting on Not now, and Escape is Not now.
 * ``children`` follow the answers.
 */
export default function KeyRequest({ request, names, onAnswered, dialog = false, children }) {
  const answer = useKeyAnswer(request, names)
  const { words, guard } = answer
  const [pasting, setPasting] = useState(false)
  const [busy, setBusy] = useState('')
  const [error, setError] = useState('')
  const denyRef = useRef(null)

  async function respond(kind) {
    setBusy(kind)
    setError('')
    try {
      await answer.send(kind)
      setPasting(false)
      onAnswered(kind)
    } catch (err) {
      setError(err.message)
      setBusy('')
    }
  }
  useDialogFocus({
    open: dialog,
    containerRef: guard.cardProps.ref,
    initialFocusRef: denyRef,
    onClose: () => { if (guard.active && !busy) respond('deny') },
  })

  const denyButton = (
    <button
      type="button"
      ref={denyRef}
      className={`settings__btn settings__btn--sm${words.allowHidden ? '' : ' settings__btn--outline'}`}
      aria-disabled={guard.active ? undefined : 'true'}
      onClick={event => { if (guard.counts(event)) respond('deny') }}
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
      onClick={event => { if (guard.counts(event)) respond('allow') }}
      disabled={Boolean(busy) || (words.needsValue && !answer.filled)}
    >
      {busy === 'allow' ? 'Allowing…' : label}
    </button>
  )
  const notices = (
    <>
      {error && <Alert color="danger" variant="soft" description={error} />}
      {guard.early !== null && <p className="settings-keys__muted" role="status">{earlyAnswerHint(guard.early)}</p>}
    </>
  )

  // Continue answers nothing, so it needs no guard; the second step's new
  // size wakes the guard again, so a double tap never lands on Save and allow.
  const second = words.needsValue && pasting
  return (
    <div
      className="settings-keys__request"
      {...(dialog ? { role: 'dialog', 'aria-modal': 'true' } : { role: 'group' })}
      aria-label={second ? words.pasteTitle : `${words.who} asks to use ${request.key}`}
      {...guard.cardProps}
    >
      {second ? (
        <>
          <KeyPasteField answer={answer} disabled={Boolean(busy)} autoFocus />
          {notices}
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
        </>
      ) : (
        <>
          <p className="settings-keys__request-lead"><Lead segments={words.lead} /></p>
          <KeyRequestBody request={request} words={words} />
          {words.allowHidden && (
            <KeyAnyway anyway={words.anyway}>
              <div className="settings-keys__actions settings-keys__actions--start">
                {allowButton(`Use ${words.savedBy}'s key`, false)}
              </div>
            </KeyAnyway>
          )}
          {notices}
          <p className="settings-keys__muted">{words.denyNote}</p>
          <div className="settings-keys__actions">
            {denyButton}
            {words.needsValue
              ? (
                <button type="button" className="settings__btn settings__btn--sm" onClick={() => setPasting(true)} disabled={Boolean(busy)}>
                  Continue
                </button>
              )
              : !words.allowHidden && allowButton('Allow', true)}
          </div>
        </>
      )}
      {children}
    </div>
  )
}
