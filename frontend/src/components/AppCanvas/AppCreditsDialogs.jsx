/**
 * The sheet a credits charge raises over the running app (AppCanvas): the
 * charge waits here for the owner's Pay. It reuses the permission panel's
 * sheet styles.
 *
 * The app's service chooses when it appears, so Pay arms only once the sheet
 * has been quiet for a short pause (any press or key on it restarts the wait)
 * and counts only a press that started after that (`useArmedPress`, rules in
 * lib/creditsPayment.js): taps meant for the app never pay by landing on a
 * sheet that slid in under them. Give the sheet a `key` per payment so the
 * next one arms afresh.
 */

import { useEffect, useId, useRef, useState } from 'react'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { appQueries } from '../../hooks/queries.js'
import {
  armPay, armingWait, paymentAmount, pressCounts, restartArming, startArming,
} from '../../lib/creditsPayment.js'
import AppIcon from '../AppIcon.jsx'

function now() {
  return typeof performance !== 'undefined' ? performance.now() : Date.now()
}

// Pay's handlers, whether it has armed yet, and `onActivity` for every press
// or key anywhere on the sheet, which restarts the wait while Pay is off.
function useArmedPress(onPress) {
  const [arming, setArming] = useState(() => startArming(now()))
  const pressStartedAt = useRef(null)
  useEffect(() => {
    if (arming.armedAt != null) return undefined
    let timer
    const wait = () => {
      timer = setTimeout(() => {
        const at = now()
        // A timer may fire a little early: wait out the rest.
        if (armingWait(arming, at) > 0) wait()
        else setArming(current => armPay(current, at))
      }, armingWait(arming, now()))
    }
    wait()
    return () => clearTimeout(timer)
  }, [arming])
  const { armedAt } = arming
  return {
    armed: armedAt != null,
    onActivity: () => setArming(current => restartArming(current, now())),
    onPointerDown: () => { pressStartedAt.current = now() },
    onClick: event => {
      // A click without a pointer (detail 0) came from the keyboard.
      const keyboard = event.detail === 0
      if (pressCounts({ armedAt, pressStartedAt: pressStartedAt.current, keyboard })) onPress()
    },
  }
}

// The sheet's header: the app's icon, a label and the question.
function CreditsSheetHead({ appId, name, eyebrow, titleId, title }) {
  const { data: apps } = appQueries.list.useQuery()
  const app = (apps || []).find(item => String(item.id) === String(appId))
  return (
    <header className="app-consent__head">
      <AppIcon className="app-consent__icon" item={app || { slug: appId }} label={name} size={null} />
      <div>
        <p className="app-consent__eyebrow">{eyebrow}</p>
        <h2 className="app-consent__title" id={titleId}>{title}</h2>
      </div>
    </header>
  )
}

// Every credits charge an app makes waits here for the owner's Pay: nothing
// leaves their credits until they have seen who is paid, for what, and how much.
// With `refusal` ({ title, message, failed }, see answerRefusalSheet), the
// payment is no longer waiting and the sheet says why: it lapsed (`failed`),
// or was answered already, so there is nothing left to pay.
export function AppCreditsPaymentDialog({
  appId, appName, payment, busy, error, refusal = null, onCancel, onPay, onClose,
}) {
  const dialogRef = useRef(null)
  const cancelRef = useRef(null)
  const titleId = useId()
  const pay = useArmedPress(onPay)
  useDialogFocus({
    containerRef: dialogRef, initialFocusRef: cancelRef, onClose: refusal ? onClose : onCancel,
  })
  const name = appName || payment.app_name || 'This app'
  const { figure, ceiling } = paymentAmount(payment)
  return (
    <div className="canvas-credits" role="dialog" aria-modal="true"
      aria-labelledby={titleId} ref={dialogRef} tabIndex={-1}
      onPointerDownCapture={pay.onActivity} onKeyDownCapture={pay.onActivity}>
      <section className="app-consent canvas-credits__sheet">
        <CreditsSheetHead appId={appId} name={name} titleId={titleId}
          eyebrow="Credits payment"
          title={refusal ? refusal.title : 'Confirm your purchase'} />
        <div className="canvas-credits__amount">
          <span className="canvas-credits__figure">{figure}</span>
          <span className="canvas-credits__unit">credits</span>
        </div>
        <ul className="app-consent__list">
          <li className="app-consent__row">
            <div>
              <strong>{payment.description || payment.action}</strong>
              <span>By @{payment.seller} · in {name}</span>
            </div>
          </li>
          <li className="app-consent__row">
            <div>
              <strong>Möbius credits</strong>
              <span>Taken from your Möbius credits balance.</span>
            </div>
          </li>
        </ul>
        {refusal ? (
          <>
            <p className={refusal.failed ? 'app-consent__error' : 'app-consent__meta'}
              role="alert">{refusal.message}</p>
            <div className="app-consent__actions">
              <button type="button" className="app-consent__btn" ref={cancelRef} onClick={onClose}>Close</button>
            </div>
          </>
        ) : (
          <>
            {ceiling && (
              <p className="app-consent__meta">
                If the creator’s price rises first, {name} pays at most {ceiling} credits.
              </p>
            )}
            <p className="app-consent__meta">Nothing is paid until you confirm.</p>
            {error && <p className="app-consent__error" role="alert">{error}</p>}
            <div className="app-consent__actions">
              <button type="button" className="app-consent__btn" ref={cancelRef}
                onClick={onCancel} disabled={busy}>Cancel</button>
              {/* Off while arming without `disabled`, which would swallow the
                  presses that must restart the wait. */}
              <button type="button" className="app-consent__btn app-consent__btn--primary"
                onPointerDown={pay.onPointerDown} onClick={pay.onClick}
                disabled={busy} aria-disabled={pay.armed ? undefined : true}>
                {busy ? 'Paying…' : `Pay ${figure} credits`}
              </button>
            </div>
          </>
        )}
      </section>
    </div>
  )
}
