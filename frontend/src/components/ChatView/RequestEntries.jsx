/* The restart and secret entries of a request-list card, each acting on its own. */

import { useEffect, useId, useRef, useState } from 'react'
import { api, jsonOrThrow } from '../../api/client.js'
import { getOnlineSnapshot } from '../../lib/connectivityStore.js'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import {
  SecureFields,
  SecureReceipts,
  secureFormAutoComplete,
  useSecureSubmission,
} from './SecureInputCard.jsx'
import { restartCardStatusDetail, restartCardStatusLabel } from './restartCard.js'
import { restartEntryOption } from './requestEntries.js'
import { INTERACTIVE_TARGET } from '../../lib/interactiveTarget.js'


const OFFLINE = 'You’re offline. Try again when you’re back online.'
const RESTART_GONE = 'This restart is no longer offered on this card. If it’s still needed, it comes back on the next card.'
const SECRET_GONE = 'This secure input is no longer offered on this card. If it’s still needed, it comes back on the next card.'


/**
 * Owner-facing copy for a Restart now press that did not go through. It is
 * chosen only after the server's record was read (`reconciled`: open, gone or
 * unknown; a press found recorded shows as requested instead, with no
 * error). It never says nothing restarted unless the server shows the
 * restart still offered and unpressed. Fixed copy only: server text never
 * reaches the card.
 */
export function restartPressError(error) {
  const status = Number(error?.status)
  const offline = !getOnlineSnapshot()
  if (error?.reconciled === 'gone') return RESTART_GONE
  if (error?.reconciled === 'open') {
    const again = offline ? 'Press Continue again when you’re back online.' : 'Press Continue again if you still want it.'
    if (status === 409 && error?.code === 'restart_action_conflict') {
      return 'This restart request is no longer active, so that press didn’t count. Ask the agent to check it again.'
    }
    if (error?.outcomeUnknown) return `Möbius didn’t confirm that press, and the restart is still offered, so it wasn’t recorded. ${again}`
    if (status >= 400 && status < 500) return `Möbius didn’t accept that press, and the restart is still offered. ${again}`
    if (offline) return `You’re offline, so that press didn’t reach Möbius. The restart is still offered. ${again}`
    return `That press didn’t reach Möbius. The restart is still offered. ${again}`
  }
  return offline
    ? 'You’re offline, so Möbius couldn’t confirm what happened to that press. Reopen this chat when you’re back online before pressing again.'
    : 'Möbius couldn’t confirm what happened to that press, and the restart may already be under way. Reopen this chat to check before pressing again.'
}


/** Fixed copy for a sealed entry's failed submit or cancel; never server text. */
export function secretActionError(error, action) {
  const status = Number(error?.status)
  if (status === 410) return SECRET_GONE
  if (status === 409) return 'This secure input is no longer open.'
  if (status === 400) return 'Those values weren’t accepted. Check them and enter them again.'
  if (!getOnlineSnapshot()) return OFFLINE
  return action === 'cancel'
    ? 'Couldn’t cancel. Please try again.'
    : 'That didn’t go through. Please enter the values again.'
}


/** A failure after which the request may still have landed. */
function outcomeMayHaveLanded(error) {
  const status = Number(error?.status)
  return !status || status >= 500
}


function SealedStatus({ state }) {
  if (!state) return null
  const text = typeof state.outcome === 'string' && state.outcome.trim()
    ? state.outcome.trim()
    : state.status === 'consuming'
      ? 'Your values went straight to the local process. Waiting for it to finish.'
      : ''
  const failed = state.status === 'failed' || state.status === 'interrupted'
  return text
    ? <p className={`qcard__entry-outcome${failed ? ' qcard__entry-outcome--failed' : ''}`} role="status">{text}</p>
    : null
}


/**
 * A secret entry: its sealed fields with their own Enter securely and Cancel.
 * Values exist only in the uncontrolled inputs until Submit takes and clears
 * them; `onState` hears the receipt's new status (never a value).
 */
export function SecretEntry({ chatId, cardId, entry, state, interactive, onState }) {
  const secure = entry.secure_input || {}
  const fields = secure.fields || []
  const open = interactive && !state
  const activeRef = useRef(true)
  useEffect(() => {
    activeRef.current = true
    return () => { activeRef.current = false }
  }, [])
  // What the receipt says now. A failed check changes nothing: the submit
  // endpoint is authoritative and answers an entry no longer waiting with its
  // status, never a second use of the values.
  const checkSavedState = () => api.secureInputs.savedState(chatId, entry.id)
    .then(response => jsonOrThrow(response, 'Could not check secure input'))
    .then(result => {
      if (activeRef.current && result.status && result.status !== 'pending') {
        onState(entry.id, {
          status: result.status,
          ...(result.outcome ? { outcome: result.outcome } : {}),
        })
      }
    })
    .catch(() => {})
  const {
    formRef, submitting, cancelling, error, submit, cancel,
  } = useSecureSubmission({
    chatId, requestId: entry.id, fields, revealFlag: false, open,
    // Cancel binds to what this card shows, as a submission does.
    shown: { question_id: cardId, request_revision: entry.request_revision },
    onStatus: status => onState(entry.id, { status }),
    describeError: secretActionError,
    // Nothing resends a sealed submit or cancel. When no verdict came back,
    // read the receipt so a request that landed shows its real state.
    afterFailure: failure => { if (outcomeMayHaveLanded(failure)) return checkSavedState() },
  })

  // Another tab, or a run that started meanwhile, may already have taken it.
  useEffect(() => {
    if (!interactive || state || !entry.id) return undefined
    void checkSavedState()
    return undefined
    // Checked once per entry while it is offered; `onState` is not identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [interactive, Boolean(state), entry.id, chatId])

  const status = state?.status || 'pending'
  const titleId = useId()
  return (
    <>
      <div className="qcard__entry-head">
        <span className="secure-card__receipt-lock qcard__entry-lock" aria-hidden="true" />
        <span>Secure input</span>
      </div>
      <div className="qcard__text" id={titleId}>{secure.title || entry.question}</div>
      {/* Once settled the receipt says what happened; the explanation of
          where values go belongs to the open form. */}
      {secure.description && !state && <div className="qcard__context">{secure.description}</div>}
      {open ? (
        <form
          ref={formRef}
          className="secure-card__form qcard__secret-form"
          autoComplete={secureFormAutoComplete(fields)}
          aria-labelledby={titleId}
          onSubmit={submit}
        >
          <SecureFields fields={fields} disabled={submitting || cancelling} />
          {error && <p className="secure-card__error" role="alert">{error}</p>}
          <div className="secure-card__actions">
            <button className="secure-card__cancel" type="button" onClick={cancel}
              disabled={submitting || cancelling}>
              {cancelling ? 'Cancelling…' : 'Cancel'}
            </button>
            <button className="secure-card__submit" type="submit" disabled={submitting || cancelling}>
              {submitting ? 'Entering…' : 'Enter securely'}
            </button>
          </div>
          <p className="qcard__entry-foot">Sent on its own · values bypass the chat and AI</p>
        </form>
      ) : (
        <>
          <SecureReceipts fields={fields} status={status} />
          <SealedStatus state={state} />
        </>
      )}
    </>
  )
}


function restartAction(entry, state) {
  const base = entry.platform_action || {}
  return {
    ...base,
    ...(state?.platform_action || {}),
    type: 'restart',
    version: 2,
    status: state?.status || base.status,
  }
}


/**
 * A restart entry, in the legacy Restart card's look: its heading and prompt,
 * the Restart now choice with its consequence, the owner's reply box, and
 * Continue. Choosing Restart now and pressing Continue sends the press, which
 * is its own request: `onPress` sends it and resolves to the entry's new
 * state. A reply written in `reply` is never a press; it travels with the
 * card's shared Submit, or with this Continue (`onSubmitReply`) when the
 * restart is the only entry the shared Submit would carry.
 */
export function RestartEntry({
  entry, state, interactive, onPress, onState, chosen = false, onChoose,
  reply, answeredText, onInternalNav, onSubmitReply, replyReady = false,
  replySubmitting = false, replyError = '', textId,
}) {
  const option = restartEntryOption(entry)
  const [pressing, setPressing] = useState(false)
  const [error, setError] = useState('')
  // Set synchronously so two presses in one tick send one request; released
  // only once the press, and any reconciliation after it, has settled.
  const pressingRef = useRef(false)
  // After an unconfirmed press whose outcome could not be checked, the button
  // stays inert: another press might restart Möbius twice.
  const [unconfirmed, setUnconfirmed] = useState(false)
  const action = restartAction(entry, state)
  const label = state ? restartCardStatusLabel(action) : ''
  const detail = state ? restartCardStatusDetail(action) : ''
  const pressable = interactive && !state && Boolean(option) && Boolean(onPress) && !unconfirmed
  const busy = pressing || replySubmitting
  const canPress = pressable && chosen
  const canReply = !chosen && interactive && !state && Boolean(onSubmitReply) && replyReady
  const optionDescriptionId = textId ? `${textId}-opt0` : undefined
  const open = !state && !answeredText && (interactive || pressing) && option

  async function press() {
    if (!canPress || pressingRef.current) return
    pressingRef.current = true
    setError('')
    setPressing(true)
    try {
      const response = await onPress()
      if (response?.state) onState(entry.id, response.state)
    } catch (failure) {
      // Only a restart the server still shows offered may be pressed again;
      // anything else might restart Möbius twice.
      if (failure?.reconciled !== 'open') setUnconfirmed(true)
      setError(restartPressError(failure))
    } finally {
      pressingRef.current = false
      setPressing(false)
    }
  }

  const choose = () => { if (interactive && !busy && !state) onChoose?.() }
  // What the entry settled reads as the legacy card's status line, above it.
  const status = label
    ? { label, detail }
    : answeredText && !state
      ? { label: restartCardStatusLabel({ type: 'restart', version: 2, status: 'responded' }),
          detail: restartCardStatusDetail({ type: 'restart', version: 2, status: 'responded' }) }
      : null
  return (
    <>
      <div className="qcard__header">Restart Möbius</div>
      <div className="qcard__text" id={textId}><StandardMarkdown text={entry.question} literalTags onInternalNav={onInternalNav} /></div>
      {/* What the entry settled takes the place its controls had, so the
          heading and prompt above it never move. */}
      {status && (
        <div className="qcard__action-status qcard__entry-settled" role="status">
          {status.label}
          {status.detail && <span className="qcard__action-detail">{status.detail}</span>}
        </div>
      )}
      {open && (
        <>
          <div className="qcard__hint">
            {action.observation?.observed_at ? 'Restart again' : 'Restart now'}{reply ? ', or reply below' : ''}
          </div>
          <div className="qcard__opts" role="radiogroup" aria-labelledby={textId}>
            <div
              className={`qcard__opt${chosen ? ' qcard__opt--on' : ''}${busy ? ' qcard__opt--static' : ''}`}
              onClick={busy ? undefined : (event) => {
                if (event.target?.closest?.(INTERACTIVE_TARGET)) return
                choose()
              }}
            >
              <div className="qcard__opt-choice"
                tabIndex={busy ? undefined : 0}
                role="radio"
                aria-checked={chosen}
                aria-disabled={busy || undefined}
                aria-describedby={option.description ? optionDescriptionId : undefined}
                onClick={busy ? undefined : choose}
                onKeyDown={busy ? undefined : event => {
                  if (event.target !== event.currentTarget || event.repeat) return
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault()
                    choose()
                  }
                }}
              >
                <span className="qcard__mark qcard__mark--radio" aria-hidden="true" />
                <span className="qcard__opt-label">{option.label || 'Restart now'}</span>
              </div>
              {option.description && <div className="qcard__opt-desc" id={optionDescriptionId}>
                <StandardMarkdown text={option.description} literalTags onInternalNav={onInternalNav} />
              </div>}
            </div>
          </div>
          {reply}
          <button
            type="button"
            className="qcard__submit qcard__submit--entry qcard__restart"
            disabled={(!canPress && !canReply) || busy}
            onClick={(event) => {
              if (chosen) press()
              else if (canReply) onSubmitReply(event?.currentTarget?.closest?.('.qcard') || null)
            }}
          >
            {busy ? 'Submitting…' : 'Continue'}
          </button>
          {/* Under Continue, so the button just pressed does not move. A
              reply sent with Continue reports its failure here too. */}
          {(error || replyError) && <div className="qcard__submit-error" role="status">{error || replyError}</div>}
        </>
      )}
      {!open && answeredText && !state && reply}
    </>
  )
}
