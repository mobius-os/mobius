/* One-use secure input with a durable prompt-only receipt. */

import { useEffect, useRef, useState } from 'react'
import { api, jsonOrThrow } from '../../api/client.js'
import './SecureInputCard.css'


function secureSettledLabel(status) {
  if (status === 'filled') return 'Provided'
  if (status === 'consuming') return 'Using securely…'
  if (status === 'interrupted') return 'Outcome unknown'
  if (status === 'completed') return 'Provided securely'
  // A failed consumer has already received the values. "Not used" would
  // incorrectly describe the input lifecycle rather than the operation.
  if (status === 'failed') return 'Used securely'
  if (status === 'cancelled' || status === 'expired') return 'Not provided'
  return ''
}


export function collectAndClearSecureFields(form, fields) {
  const inputs = new Map(
    Array.from(form.querySelectorAll('input[data-secure-field]'))
      .map(input => [input.dataset.secureField, input]),
  )
  const values = {}
  for (const field of fields || []) {
    values[field.name] = String(inputs.get(field.name)?.value || '')
  }

  // Chrome can treat removal of a filled password form as a successful login.
  // Clear and blur before the server can publish a state that replaces the form.
  wipeSecureFields(form)
  return values
}


/**
 * Empty and blur every sealed input of a form. The one wipe for every way a
 * form stops holding values: submit, a successful cancel, the request no
 * longer being open, and unmount. Safe on a form React has detached.
 */
export function wipeSecureFields(form) {
  if (!form) return
  form.reset()
  for (const input of form.querySelectorAll('input[data-secure-field]')) {
    input.value = ''
    input.blur()
  }
}


function declaredCredentialHint(field) {
  const hint = field?.autocomplete
  return typeof hint === 'string' && hint !== 'off' ? hint : null
}


/** Whether these prompts may use the browser's password manager. */
export function secureFormAutoComplete(fields) {
  return (fields || []).some(declaredCredentialHint) ? 'on' : 'off'
}


/**
 * One uncontrolled input per sealed prompt. Values live only in these DOM
 * inputs until `collectAndClearSecureFields` takes and clears them; nothing
 * here keeps them in React state, a draft or the transcript.
 */
export function SecureFields({ fields, disabled }) {
  return (fields || []).map(field => {
    const credentialHint = declaredCredentialHint(field)
    const genericMaskedField = field.type !== 'text' && !credentialHint
    return (
      <label className="secure-card__field" key={field.name}>
        <span>{field.label}</span>
        <input
          name={credentialHint ? field.name : undefined}
          // Chromium deliberately ignores autocomplete="off" on
          // password fields. Generic secrets are ordinary text inputs
          // with visual masking; only explicitly declared owner
          // credentials enter the browser's password-manager flow.
          type={genericMaskedField ? 'text' : (field.type === 'text' ? 'text' : 'password')}
          autoComplete={credentialHint || 'off'}
          required
          disabled={disabled}
          data-secure-field={field.name}
          data-secure-masked={genericMaskedField ? 'true' : undefined}
          data-chat-inline-editor="secure-input"
        />
      </label>
    )
  })
}


/** The settled form: each prompt with what happened to it, never a value. */
export function SecureReceipts({ fields, status }) {
  return (
    <div className="secure-card__receipts" role="status">
      {(fields || []).map(field => (
        <div className="secure-card__receipt" key={field.name}>
          <span className="secure-card__receipt-prompt">
            <span className="secure-card__receipt-lock" aria-hidden="true" />
            <span>{field.label}</span>
          </span>
          <strong>{secureSettledLabel(status) || 'Not provided'}</strong>
        </div>
      ))}
    </div>
  )
}


function messageOf(error, fallback) {
  return error?.message || fallback
}


/**
 * Submit and Cancel for one sealed request. The submitted values are read
 * from the form, cleared from it before the request is sent, and dropped when
 * it settles; `onStatus` hears the server's resulting status.
 *
 * `describeError(error, action)` turns a failure into owner-facing copy.
 * `revealFlag` keeps the original card's body, which always names
 * `reveal_confirmed`; a request-list entry sends `{fields}` only. `shown`
 * is what Cancel names: a list entry's card id and request revision.
 */
export function useSecureSubmission({
  chatId, requestId, fields, reveal = false, revealFlag = true, open,
  onStatus, describeError, afterFailure, shown,
}) {
  const formRef = useRef(null)
  const [submitting, setSubmitting] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [error, setError] = useState('')

  // Values typed but never submitted go when the form does: the request was
  // superseded or settled elsewhere (open turns false), or the card unmounts.
  // The form node is captured now: React has usually cleared the ref by
  // cleanup time, so the ref is only the fallback.
  useEffect(() => {
    if (!open) return undefined
    const form = formRef.current
    // The fallback deliberately reads the ref late; the captured node wins.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    return () => wipeSecureFields(form || formRef.current)
  }, [open])

  async function submit(event) {
    event.preventDefault()
    if (!open || submitting) return
    const form = formRef.current
    if (!form?.reportValidity()) return

    const revealConfirmed = reveal
      && form.elements.reveal_confirmed?.checked === true
    if (reveal && !revealConfirmed) {
      setError('Confirm that these values may be sent to the AI provider.')
      return
    }

    const values = collectAndClearSecureFields(form, fields)
    setError('')
    setSubmitting(true)
    try {
      const result = await jsonOrThrow(
        await api.secureInputs.submit(chatId, requestId, revealFlag
          ? { fields: values, reveal_confirmed: revealConfirmed }
          : { fields: values }),
        'Secure input failed',
      )
      onStatus(result.status || 'filled')
    } catch (submitError) {
      setError(describeError
        ? describeError(submitError, 'submit')
        : messageOf(submitError, 'Secure input failed. Please enter the values again.'))
      // Never resent: the caller may only read what the server recorded.
      await afterFailure?.(submitError, 'submit')
    } finally {
      // Submitted fields are ordinary JS memory and become unreachable here.
      for (const key of Object.keys(values)) values[key] = ''
      setSubmitting(false)
    }
  }

  async function cancel() {
    if (!open || submitting || cancelling) return
    setError('')
    setCancelling(true)
    try {
      await jsonOrThrow(
        await api.secureInputs.cancel(chatId, requestId, shown),
        'Could not cancel',
      )
      // Values typed but never submitted leave with the form.
      wipeSecureFields(formRef.current)
      onStatus('cancelled')
    } catch (cancelError) {
      setError(describeError
        ? describeError(cancelError, 'cancel')
        : messageOf(cancelError, 'Could not cancel this input.'))
      await afterFailure?.(cancelError, 'cancel')
    } finally {
      setCancelling(false)
    }
  }

  return { formRef, submitting, cancelling, error, setError, submit, cancel }
}


export default function SecureInputCard({ block, chatId, interactive = false }) {
  const [localStatus, setLocalStatus] = useState(block.status || 'pending')
  const reveal = block.mode === 'reveal'
  const blockStatus = block.status || 'pending'
  const status = blockStatus === 'pending' ? localStatus : blockStatus
  const open = status === 'pending' && interactive
  const {
    formRef, submitting, cancelling, error, setError, submit, cancel,
  } = useSecureSubmission({
    chatId, requestId: block.request_id, fields: block.fields, reveal, open,
    onStatus: setLocalStatus,
  })

  useEffect(() => {
    if (block.status && block.status !== 'pending') {
      setLocalStatus(block.status)
    }
  }, [block.status])

  useEffect(() => {
    if (!block.saved || !interactive) return
    let active = true
    api.secureInputs.savedState(chatId, block.request_id)
      .then(response => jsonOrThrow(response, 'Could not check secure input'))
      .then(result => {
        if (active && result.status && result.status !== 'pending') {
          setLocalStatus(result.status)
        }
      })
      .catch(() => {
        if (active) setError('Could not check this request. Reopen the chat before submitting again.')
      })
    return () => { active = false }
  }, [block.saved, block.request_id, chatId, interactive, setError])

  return (
    <section
      className={`secure-card${reveal ? ' secure-card--reveal' : ''}`}
      aria-label={block.title || 'Secure input'}
    >
      <header className="secure-card__head">
        <span className="secure-card__lock" aria-hidden="true" />
        <div>
          <h3 className="secure-card__title">{block.title || 'Secure input'}</h3>
        </div>
      </header>

      <p className="secure-card__description">
        {block.description || (
          reveal
            ? 'These values will be sent to the AI provider for this turn.'
            : 'These values go directly to a local process and bypass the AI provider.'
        )}
      </p>

      {open ? (
        <form
          ref={formRef}
          className="secure-card__form"
          autoComplete={secureFormAutoComplete(block.fields)}
          onSubmit={submit}
        >
          <SecureFields fields={block.fields} disabled={submitting} />

          {reveal && (
            <label className="secure-card__consent">
              <input
                type="checkbox"
                name="reveal_confirmed"
                value="yes"
                disabled={submitting}
              />
              <span>
                I understand these values will be sent to the AI provider.
                Möbius will omit them from its own chat and logs.
              </span>
            </label>
          )}

          {error && <p className="secure-card__error" role="alert">{error}</p>}
          <div className="secure-card__actions">
            <button
              className="secure-card__cancel"
              type="button"
              onClick={cancel}
              disabled={submitting || cancelling}
            >
              {cancelling ? 'Cancelling…' : 'Cancel'}
            </button>
            <button className="secure-card__submit" type="submit" disabled={submitting || cancelling}>
              {submitting
                ? 'Entering…'
                : reveal ? 'Reveal for this turn' : 'Enter securely'}
            </button>
          </div>
        </form>
      ) : (
        <SecureReceipts fields={block.fields} status={status} />
      )}

      {block.outcome && <p className="secure-card__description" role="status">{block.outcome}</p>}
      <p className="secure-card__foot">
        {reveal
          ? (open
              ? 'Explicit reveal sends values to AI; Möbius omits them from its transcript.'
              : status === 'failed'
                ? 'Operation failed · revealed values omitted from the Möbius transcript'
                : 'Receipt saved · revealed values omitted from the Möbius transcript')
          : (open
              ? 'One-time entry · values bypass the chat and AI'
              : status === 'failed'
                ? 'Operation failed · entered values omitted'
                : 'Receipt saved · entered values omitted')}
      </p>
    </section>
  )
}
