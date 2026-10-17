import { useCallback, useEffect, useRef, useState } from 'react'
import { Phone, X } from '@openai/apps-sdk-ui/components/Icon'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { api } from '../../api/client.js'
import {
  disableNativePush,
  enableNativePush,
  isNativePushAvailable,
  isThisPhone,
} from '../../lib/nativePush.js'

function lastOpened(iso) {
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function phoneName(device) {
  return isThisPhone(device.tokenTail) ? 'This phone' : `Phone ···${device.tokenTail}`
}

const DENIED = "Notifications are off for Möbius in this phone's settings. Allow them there, then turn on again."

/**
 * Phones registered for native push (the Möbius mobile app). Inside the app,
 * the owner turns notifications on or off for this phone here; nothing
 * registers until then. Elsewhere the list is read-only and says where to
 * change it.
 */
export default function PhoneNotifications({ active = true }) {
  const [devices, setDevices] = useState([])
  const [confirming, setConfirming] = useState(null)
  const [removing, setRemoving] = useState(false)
  // While a change is in flight the switch shows where it is going.
  const [switching, setSwitching] = useState(null)
  const [message, setMessage] = useState('')
  const dialogRef = useRef(null)
  const inApp = isNativePushAvailable()
  const thisPhoneOn = devices.some((device) => isThisPhone(device.tokenTail))

  const load = useCallback(async () => {
    try {
      const response = await api.push.devices.list()
      if (!response?.ok) return
      const body = await response.json()
      setDevices(Array.isArray(body) ? body : [])
    } catch {
      // Settings stays usable; the section simply shows nothing new.
    }
  }, [])

  useEffect(() => {
    if (active) load()
  }, [active, load])

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (confirming && !dialog.open) dialog.showModal?.()
    if (!confirming && dialog.open) dialog.close()
  }, [confirming])

  const turnOn = async () => {
    setSwitching('on')
    setMessage('')
    const result = await enableNativePush()
    if (result.status === 'denied') setMessage(DENIED)
    else if (result.status === 'error') setMessage(result.message)
    await load()
    setSwitching(null)
  }

  const turnOff = async () => {
    setSwitching('off')
    setMessage((await disableNativePush()) || '')
    await load()
    setSwitching(null)
  }

  // The × on this phone's card is Turn off, behind a confirmation.
  const confirmTurnOff = async () => {
    setRemoving(true)
    setMessage((await disableNativePush()) || '')
    setRemoving(false)
    setConfirming(null)
    load()
  }

  return (
    <section className="settings__section settings__section--compact">
      <div className="settings__row">
        <span className="settings__label">Phone notifications</span>
        {inApp && (
          <Switch
            className="settings-phone-switch"
            aria-label="Notifications on this phone"
            checked={switching ? switching === 'on' : thisPhoneOn}
            disabled={!!switching}
            onCheckedChange={(next) => (next ? turnOn() : turnOff())}
          />
        )}
      </div>
      {message ? (
        <p className="settings__subtext settings__subtext--tight" role="status">{message}</p>
      ) : inApp ? (
        !thisPhoneOn && (
          <p className="settings__subtext settings__subtext--tight">
            Get Möbius notifications on this phone, even when the app is closed.
          </p>
        )
      ) : (
        <p className="settings__subtext settings__subtext--tight">
          {devices.length > 0
            ? 'These phones get your Möbius notifications. To turn them on or off for a phone, open Settings in the Möbius app on that phone.'
            : 'No phones connected. To get Möbius notifications on your phone, open Settings in the Möbius app there and switch them on.'}
        </p>
      )}
      {devices.length > 0 && (
        <div className="settings-phones">
          {devices.map((device) => (
            <div className="settings-phone" key={device.id}>
              <span className="settings-phone__icon" aria-hidden="true"><Phone /></span>
              <span className="settings-phone__main">
                <span className="settings-phone__name">{phoneName(device)}</span>
                <span className="settings-phone__meta">Last opened {lastOpened(device.registeredAt)}</span>
              </span>
              {inApp && isThisPhone(device.tokenTail) && (
                <button
                  className="settings-phone__remove"
                  type="button"
                  aria-label="Turn off notifications on this phone"
                  onClick={() => setConfirming(device)}
                >
                  <X />
                </button>
              )}
            </div>
          ))}
        </div>
      )}
      <dialog
        ref={dialogRef}
        className="settings-phone-confirm"
        aria-labelledby="settings-phone-confirm-title"
        onCancel={(event) => { event.preventDefault(); if (!removing) setConfirming(null) }}
      >
        <h3 id="settings-phone-confirm-title" className="settings-phone-confirm__title">
          Turn off notifications on this phone?
        </h3>
        <p className="settings__subtext settings__subtext--tight">
          This phone stops getting Möbius notifications. You can turn them on again here.
        </p>
        <div className="settings-phone-confirm__actions">
          <button
            className="settings__btn settings__btn--outline settings__btn--sm"
            type="button"
            disabled={removing}
            onClick={() => setConfirming(null)}
          >
            Cancel
          </button>
          <button
            className="settings__btn settings__btn--sm"
            type="button"
            disabled={removing}
            onClick={confirmTurnOff}
          >
            {removing ? 'Turning off…' : 'Turn off'}
          </button>
        </div>
      </dialog>
    </section>
  )
}
