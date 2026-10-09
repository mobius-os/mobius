/* Möbius model access and picker visibility inside the Settings provider row. */
import { useCallback, useEffect, useState } from 'react'
import { identityRequest } from './identity-client.js'
import './MobiusProviderAccess.css'

const dollars = units => new Intl.NumberFormat(undefined, {
  style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2,
}).format(units / 1_000_000)

const rate = value => new Intl.NumberFormat(undefined, {
  style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 4,
}).format(value)

function accessStatus(access, error) {
  if (!access) return error ? 'Unavailable' : 'Checking…'
  if (access.agent_access === 'signed_out') return 'Sign in needed'
  if (access.agent_access !== 'available') return 'Unavailable'
  return access.balance.available_units > 0 || access.trial.state === 'ready' ? 'Available' : 'No credit'
}

export default function MobiusProviderAccess({ token, onVisibilityChange }) {
  const [access, setAccess] = useState(null)
  const [accessError, setAccessError] = useState('')
  const [accessBusy, setAccessBusy] = useState(false)
  const [visible, setVisible] = useState(null)
  const [visibilityError, setVisibilityError] = useState('')
  const [visibilityBusy, setVisibilityBusy] = useState(false)

  const loadAccess = useCallback(async () => {
    setAccessError('')
    try {
      setAccess(await identityRequest(token, '/agent'))
    } catch (error) {
      setAccessError(error.message)
    }
  }, [token])

  const loadVisibility = useCallback(async () => {
    setVisibilityError('')
    try {
      const result = await identityRequest(token, '/api/auth/providers/mobius/enabled')
      setVisible(result.enabled)
    } catch (error) {
      setVisibilityError(error.message)
    }
  }, [token])

  useEffect(() => { void loadAccess(); void loadVisibility() }, [loadAccess, loadVisibility])

  const setVisibility = async next => {
    if (visibilityBusy) return
    setVisibilityBusy(true)
    setVisibilityError('')
    try {
      const result = await identityRequest(token, '/api/auth/providers/mobius/enabled', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled: next }),
      })
      setVisible(result.enabled)
      onVisibilityChange?.()
    } catch (error) {
      setVisibilityError(error.message)
    } finally {
      setVisibilityBusy(false)
    }
  }

  const activate = async () => {
    if (accessBusy) return
    setAccessBusy(true)
    setAccessError('')
    try {
      setAccess(await identityRequest(token, '/agent/trial', { method: 'POST' }))
    } catch (error) {
      setAccessError(error.code === 'trial_fund_exhausted'
        ? 'Trial capacity is unavailable right now. Your account is still ready.'
        : error.message)
    } finally {
      setAccessBusy(false)
    }
  }

  return <div className="settings-mobius-access">
    <label className="settings-mobius-access__visibility">
      <span>
        <strong>Show Möbius models in pickers</strong>
        <small>Hiding them also pauses existing Möbius chats until you turn them back on.</small>
      </span>
      <input type="checkbox" checked={visible === true} disabled={visible === null || visibilityBusy} onChange={event => void setVisibility(event.target.checked)} />
    </label>
    {visibilityError && <p className="settings-mobius-access__error" role="alert">{visibilityError} {visible === null && <button type="button" onClick={loadVisibility}>Retry</button>}</p>}

    <div className="settings-mobius-access__row">
      <span>Model access</span>
      <strong>{accessStatus(access, accessError)}</strong>
    </div>
    {access?.agent_access === 'available' && <>
      {access.trial.state === 'ready' && <button type="button" className="settings-mobius-access__action" disabled={accessBusy} onClick={activate}>{accessBusy ? 'Activating…' : 'Activate trial credit'}</button>}
      <details className="settings-mobius-access__prices">
        <summary>Access details and token rates</summary>
        <div>
          <div className="settings-mobius-access__row"><span>Available credit</span><strong>{dollars(access.balance.available_units)}</strong></div>
          {access.trial.state !== 'ready' && access.balance.available_units <= 0 && <p className="settings-mobius-access__note">Credit is empty. Ask your Möbius provider to add more.</p>}
          <p className="settings-mobius-access__note">{access.retention.notice}</p>
          {access.models.map(model => <div className="settings-mobius-access__price" key={model.id}>
            <strong>{model.name}</strong>
            <span>{rate(model.pricing.input)} input · {rate(model.pricing.cached_input)} cached · {rate(model.pricing.output)} output</span>
          </div>)}
          <small>Rates are USD per million tokens.</small>
        </div>
      </details>
    </>}
    {accessError && <p className="settings-mobius-access__error" role="alert">{accessError} <button type="button" onClick={loadAccess}>Retry</button></p>}
  </div>
}
