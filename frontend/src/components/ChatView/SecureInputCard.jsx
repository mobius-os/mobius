/* One-use secure input with a durable prompt-only receipt. */

import { useEffect, useRef, useState } from 'react'
import { api, jsonOrThrow } from '../../api/client.js'
import './SecureInputCard.css'


function settledLabel(status) {
  if (status === 'filled') return 'Provided'
  if (status === 'consuming') return 'Using securely…'
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
  form.reset()
  for (const input of inputs.values()) {
    input.value = ''
    input.blur()
  }
  return values
}


function declaredCredentialHint(field) {
  const hint = field?.autocomplete
  return typeof hint === 'string' && hint !== 'off' ? hint : null
}


export default function SecureInputCard({ block, chatId, interactive = false }) {
  const formRef = useRef(null)
  const [localStatus, setLocalStatus] = useState(block.status || 'pending')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const reveal = block.mode === 'reveal'
  const blockStatus = block.status || 'pending'
  const status = blockStatus === 'pending' ? localStatus : blockStatus
  const open = status === 'pending' && interactive
  const credentialForm = (block.fields || []).some(declaredCredentialHint)

  useEffect(() => {
    if (block.status && block.status !== 'pending') {
      setLocalStatus(block.status)
    }
  }, [block.status])

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

    const fields = collectAndClearSecureFields(form, block.fields)
    setError('')
    setSubmitting(true)
    try {
      await jsonOrThrow(
        await api.secureInputs.submit(chatId, block.request_id, {
          fields,
          reveal_confirmed: revealConfirmed,
        }),
        'Secure input failed',
      )
      setLocalStatus('filled')
    } catch (submitError) {
      setError(
        submitError?.message
        || 'Secure input failed. Please enter the values again.',
      )
    } finally {
      // Submitted fields are ordinary JS memory and become unreachable here.
      for (const key of Object.keys(fields)) fields[key] = ''
      setSubmitting(false)
    }
  }

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
          autoComplete={credentialForm ? 'on' : 'off'}
          onSubmit={submit}
        >
          {(block.fields || []).map(field => {
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
                  disabled={submitting}
                  data-secure-field={field.name}
                  data-secure-masked={genericMaskedField ? 'true' : undefined}
                  data-chat-inline-editor="secure-input"
                />
              </label>
            )
          })}

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
          <button className="secure-card__submit" type="submit" disabled={submitting}>
            {submitting
              ? 'Entering…'
              : reveal ? 'Reveal for this turn' : 'Enter securely'}
          </button>
        </form>
      ) : (
        <div className="secure-card__receipts" role="status">
          {(block.fields || []).map(field => (
            <div className="secure-card__receipt" key={field.name}>
              <span className="secure-card__receipt-prompt">
                <span className="secure-card__receipt-lock" aria-hidden="true" />
                <span>{field.label}</span>
              </span>
              <strong>{settledLabel(status) || 'Not provided'}</strong>
            </div>
          ))}
        </div>
      )}

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
