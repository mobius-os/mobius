/**
 * The sheet a credits charge raises over the running app (AppCanvas): the
 * charge waits here for the owner's Pay. It reuses the permission panel's
 * sheet styles.
 *
 * The app's service chooses when it appears, so Pay arms only after a short
 * pause and counts only a press that started after that (`useArmedPress`,
 * rules in lib/creditsPayment.js): a tap meant for the app never pays by
 * landing on a sheet that just slid in under it. Give the sheet a `key` per
 * payment so the next one arms afresh.
 */

import { useEffect, useId, useRef, useState } from 'react'
import useDialogFocus from '../../hooks/useDialogFocus.js'
import { appQueries } from '../../hooks/queries.js'
import { PRESS_ARM_MS, paymentAmount, pressCounts } from '../../lib/creditsPayment.js'
import AppIcon from '../AppIcon.jsx'

function now() {
  return typeof performance !== 'undefined' ? performance.now() : Date.now()
}

// Pay's handlers, and whether it has armed yet.
function useArmedPress(onPress) {
  const [armedAt, setArmedAt] = useState(null)
  const pressStartedAt = useRef(null)
  useEffect(() => {
    const timer = setTimeout(() => setArmedAt(now()), PRESS_ARM_MS)
    return () => clearTimeout(timer)
  }, [])
  return {
    armed: armedAt != null,
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
// With `refusal`, the payment did not go through and the sheet says why; it is
// no longer waiting, so there is nothing left to pay.
export function AppCreditsPaymentDialog({
  appId, appName, payment, busy, error, refusal = '', onCancel, onPay, onClose,
}) {
  const dialogRef = useRef(null)
  const cancelRef = useRef(null)
  const titleId = useId()
  const pay = useArmedPress(onPay)
  useDialogFocus({
    containerRef: dialogRef, initialFocusRef: cancelRef, onClose: refusal ? onClose : onCancel,
  })
  const name = appName || payment.app_name || 'This app'
  const byCard = payment.method === 'card'
  const { figure, unit, ceiling } = paymentAmount(payment)
  const shownFigure = byCard ? figure : `${figure} credits`
  return (
    <div className="canvas-credits" role="dialog" aria-modal="true"
      aria-labelledby={titleId} ref={dialogRef} tabIndex={-1}>
      <section className="app-consent canvas-credits__sheet">
        <CreditsSheetHead appId={appId} name={name} titleId={titleId}
          eyebrow={byCard ? 'Card payment' : 'Credits payment'}
          title={refusal ? 'This payment didn’t go through' : 'Confirm your purchase'} />
        <div className="canvas-credits__amount">
          <span className="canvas-credits__figure">{figure}</span>
          <span className="canvas-credits__unit">{unit}</span>
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
              <strong>{byCard ? 'Card' : 'Möbius credits'}</strong>
              <span>{byCard
                ? 'You finish on Stripe’s secure page.'
                : 'Taken from your Möbius credits balance.'}</span>
            </div>
          </li>
        </ul>
        {refusal ? (
          <>
            <p className="app-consent__error" role="alert">{refusal}</p>
            <div className="app-consent__actions">
              <button type="button" className="app-consent__btn" ref={cancelRef} onClick={onClose}>Close</button>
            </div>
          </>
        ) : (
          <>
            {ceiling && (
              <p className="app-consent__meta">
                If the creator’s price rises first, {name} pays at most {byCard ? ceiling : `${ceiling} credits`}.
              </p>
            )}
            <p className="app-consent__meta">{byCard
              ? 'You get it as soon as it’s paid, so it can’t be cancelled after.'
              : 'Nothing is paid until you confirm.'}</p>
            {error && <p className="app-consent__error" role="alert">{error}</p>}
            <div className="app-consent__actions">
              <button type="button" className="app-consent__btn" ref={cancelRef}
                onClick={onCancel} disabled={busy}>Cancel</button>
              <button type="button" className="app-consent__btn app-consent__btn--primary"
                onPointerDown={pay.onPointerDown} onClick={pay.onClick} disabled={busy || !pay.armed}>
                {busy ? (byCard ? 'Opening Stripe…' : 'Paying…') : `Pay ${shownFigure}`}
              </button>
            </div>
          </>
        )}
      </section>
    </div>
  )
}
