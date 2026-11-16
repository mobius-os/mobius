/* One app's or agent's key request card, as Settings › Keys, a chat and an app's window show it. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { api, jsonOrThrow } from '../../api/client.js'
import {
  keyAnswerActiveAt, keyAnswerClickCounts, keyAnswerMoved, keyAnswerPlace, keyAnswerPlaceChanged, keyAnswerPressed,
  keyAnswerShown,
} from '../../lib/keyAnswerGuard.js'
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
 * Keeps the answers asleep until the card has been still and untouched for a
 * moment (keyAnswerGuard.js). ``cardRef`` goes on the card; ``onPressCapture``
 * hears every press on it, before any of its controls. The card's place is
 * checked when it or anything around it changes size, on any scroll and at
 * each press, so a card pushed or slid into place under the pointer waits
 * again; scrolling the card yourself does too. Callers key each card by
 * ``requestFormKey``, so a replacement request is a new card and waits again.
 */
function useAnswerGuard({ enabled }) {
  const cardRef = useRef(null)
  const place = useRef(null)
  const [shownAt] = useState(() => performance.now())
  const state = useRef(keyAnswerShown(shownAt))
  const [activeAt, setActiveAt] = useState(() => keyAnswerActiveAt(keyAnswerShown(shownAt)))
  const [active, setActive] = useState(false)

  useEffect(() => {
    const wait = activeAt - performance.now()
    if (wait <= 0) { setActive(true); return undefined }
    setActive(false)
    const timer = setTimeout(() => setActive(true), wait)
    return () => clearTimeout(timer)
  }, [activeAt])

  const noteMove = useCallback(() => {
    const node = cardRef.current
    if (!node) return
    const now = keyAnswerPlace(node.getBoundingClientRect())
    if (keyAnswerPlaceChanged(place.current, now)) {
      state.current = keyAnswerMoved(state.current, performance.now())
      setActiveAt(keyAnswerActiveAt(state.current))
    }
    place.current = now
  }, [])

  useEffect(() => {
    const node = cardRef.current
    if (!enabled || !node) return undefined
    const check = () => noteMove()
    check()
    const resized = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(check)
    for (let element = node; element && resized; element = element.parentElement) resized.observe(element)
    window.addEventListener('scroll', check, { capture: true, passive: true })
    window.addEventListener('resize', check)
    return () => {
      resized?.disconnect()
      window.removeEventListener('scroll', check, { capture: true })
      window.removeEventListener('resize', check)
    }
  }, [enabled, noteMove])

  return {
    cardRef,
    active,
    onPressCapture: event => {
      // A move nothing reported yet counts as happening now.
      noteMove()
      state.current = keyAnswerPressed(state.current, event)
      setActiveAt(keyAnswerActiveAt(state.current))
    },
    counts: event => keyAnswerClickCounts(state.current, event),
  }
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
  const guard = useAnswerGuard({ enabled: !readOnly })

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

  return (
    <div
      ref={guard.cardRef}
      className="settings-keys__request"
      role="group"
      aria-label={`${words.who} asks to use ${request.key}`}
      onPointerDownCapture={guard.onPressCapture}
    >
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
