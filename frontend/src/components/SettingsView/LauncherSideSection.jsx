/* Settings › Accounts › Möbius: a developer's switch between the production and staging account service. Renders nothing unless this Möbius has a staging configuration. */
import { useEffect, useState } from 'react'
import { api, jsonOrThrow } from '../../api/client.js'
import { useSideSwitchWatch } from './useSideSwitchWatch.js'

const LABEL = { production: 'Production', staging: 'Staging' }

const other = side => (side === 'staging' ? 'production' : 'staging')

export default function LauncherSideSection() {
  const [status, setStatus] = useState(null)
  const [confirming, setConfirming] = useState(false)
  const [target, setTarget] = useState(null)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)

  useEffect(() => {
    let live = true
    api.platform.launcherSide().then(jsonOrThrow)
      .then(value => { if (live) setStatus(value) })
      .catch(() => {})
    return () => { live = false }
  }, [])

  const stuck = useSideSwitchWatch(target)

  if (!status?.available) return null
  const next = other(status.side)

  const switchSide = async () => {
    setError('')
    setPending(true)
    try {
      await jsonOrThrow(await api.platform.chooseLauncherSide(next))
      setTarget(next)
    } catch (failure) {
      setError(failure.message || 'The switch did not start.')
    } finally {
      setPending(false)
    }
  }

  const restarting = stuck
    ? 'Möbius has not come back on the chosen side yet. Reload this page to check.'
    : `Restarting on ${LABEL[target]?.toLowerCase()}. This page reloads when Möbius is back, in about a minute.`
  const notice = target
    ? restarting
    : confirming ? 'Switching restarts this Möbius, which takes about a minute and interrupts running chats.' : ''

  return (
    <section className="settings-group" aria-labelledby="mobius-account-side">
      <div className="settings-group__head">
        <div className="settings-group__heading">
          <h2 className="settings-group__title" id="mobius-account-side">Environment</h2>
          <p className="settings-group__desc">Which mobius.you this Möbius uses for sign-in, models, credits, the Store and notifications.</p>
        </div>
      </div>
      <div className="settings-list">
        <div className="settings-item">
          <span className="settings-item__body">
            <span className="settings-item__title">{LABEL[status.side]}</span>
            <span className="settings-item__sub">In use now</span>
          </span>
          <span className="settings-item__trail">
            <button
              type="button"
              className="settings__btn settings__btn--outline settings__btn--sm"
              disabled={confirming || Boolean(target)}
              onClick={() => setConfirming(true)}
            >
              Switch to {LABEL[next].toLowerCase()}
            </button>
          </span>
        </div>
        <div className="settings-item-panel" hidden={!notice}>
          <p className="settings-item-panel__text" role="status" aria-live="polite">{notice}</p>
          {confirming && !target && (
            <div className="settings-actions">
              <button type="button" className="settings__btn settings__btn--sm" disabled={pending} onClick={switchSide}>
                Restart on {LABEL[next].toLowerCase()}
              </button>
              <button type="button" className="settings__btn settings__btn--sm settings__btn--outline" disabled={pending} onClick={() => setConfirming(false)}>
                Cancel
              </button>
            </div>
          )}
        </div>
        {error && <p className="settings-message settings-message--error" role="alert">{error}</p>}
      </div>
    </section>
  )
}
