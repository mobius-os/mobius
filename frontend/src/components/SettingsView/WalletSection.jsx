/**
 * The credits service's wallet for app purchases, distinct from model credit,
 * on the Credits page. Buying opens under the balance from Add credits; the
 * history lives in ActivityWindow, last on the page.
 */
import { useEffect, useRef, useState } from 'react'
import { ArrowBottomLeftSm, ArrowTopRightSm } from '@openai/apps-sdk-ui/components/Icon'
import { activityLabel, canReconnect, money } from '../../lib/creditsWallet.js'
import './WalletSection.css'

const WAIVER = 'I want my credits straight away, and I understand that I lose my right '
  + 'to cancel once they have been delivered.'

export default function WalletSection({
  summary, terms, busy = false, notice = null, refusal = null,
  onBuy = () => {}, onReconnect = () => {},
}) {
  const [buying, setBuying] = useState(false)
  const addButton = useRef(null)
  // A finished purchase closes the amounts again.
  useEffect(() => { if (notice === 'paid') setBuying(false) }, [notice])
  const balance = summary?.balance
  const owed = typeof balance?.balance === 'string' && balance.balance.startsWith('-')
  const canBuy = Boolean(balance && terms?.amounts_cents?.length)
  const notices = {
    checking: 'Checking your payment…',
    paid: 'Payment received. Your credits are in.',
    late: "Stripe has not confirmed that payment yet. If you paid, your credits appear within a minute or two — please don't buy again.",
    paused: 'Payment checking has stopped for now. If you paid, return to Settings to refresh your balance before buying again.',
  }

  return (
    <section className="settings-group" aria-labelledby="settings-credits-title">
      <div className="settings-group__head">
        <div className="settings-group__heading">
          <h2 className="settings-group__title" id="settings-credits-title">Credits wallet</h2>
          <p className="settings-group__desc">Pays for apps that charge per use.</p>
        </div>
      </div>
      <div className="settings-list">
        <div className="settings-item">
          <span className="settings-item__body">
            <span className="settings-item__title settings-item__title--wrap">
              {balance
                ? owed ? `You owe ${balance.balance.slice(1)} credits` : `${balance.available} credits available`
                : 'Balance unavailable'}
            </span>
            <span className="settings-item__sub settings-item__sub--wrap">
              {balance
                ? `Kept by ${summary.service_host}. Credits never expire.`
                : summary?.error?.message
                  ? `Your balance could not be read just now: ${summary.error.message}`
                  : 'Your balance could not be read just now. Try again in a moment.'}
            </span>
          </span>
          <span className="settings-item__trail">
            {owed && <span className="settings-badge settings-badge--warning">Owed</span>}
            {canBuy && (
              <button
                ref={addButton}
                type="button"
                className="settings__btn settings__btn--outline settings__btn--sm"
                aria-expanded={buying}
                aria-controls={buying ? 'settings-credits-buy' : undefined}
                onClick={() => setBuying(open => !open)}
              >
                Add credits
              </button>
            )}
          </span>
        </div>
        {!balance && canReconnect(summary) && (
          <div className="settings-item settings-item--block">
            <div className="settings-actions">
              <ConnectButton busy={busy} label="Reconnect credits" onClick={onReconnect} />
            </div>
          </div>
        )}
        {canBuy && buying ? (
          // A choice or waiver for one service never carries over to another.
          <WalletBuyForm key={terms.pairing} terms={terms} busy={busy} onBuy={onBuy} onCancel={() => { setBuying(false); addButton.current?.focus() }} />
        ) : null}
      </div>
      {notices[notice] && <p className="settings-message" role="status">{notices[notice]}</p>}
      {refusal && <p className="settings-message settings-message--error" role="alert">{refusal}</p>}
    </section>
  )
}

/** One line of wallet activity: which way the money went, what, when, how much. */
export function ActivityRow({ entry, when }) {
  const out = String(entry.amount).startsWith('-')
  const Arrow = out ? ArrowTopRightSm : ArrowBottomLeftSm
  return (
    <li className="settings-item">
      <span className={`settings-item__icon settings-item__icon--round wallet-activity__icon${out ? '' : ' wallet-activity__icon--in'}`} aria-hidden="true">
        <Arrow width={16} height={16} />
      </span>
      <span className="settings-item__body">
        <span className="settings-item__title">{activityLabel(entry)}</span>
        <span className="settings-item__sub">{when}</span>
      </span>
      <span className={`wallet-activity__amount${out ? '' : ' wallet-activity__amount--in'}`}>
        <span className="wallet-activity__sr">{out ? 'Spent ' : 'Added '}</span>
        {out ? `−${String(entry.amount).slice(1)}` : `+${entry.amount}`}
      </span>
    </li>
  )
}

/** Credits for a Möbius launched from mobius.you that is not connected yet. */
export function WalletConnect({ busy = false, refusal = null, onConnect = () => {} }) {
  return (
    <section className="settings-group" aria-labelledby="settings-credits-title">
      <div className="settings-group__head">
        <div className="settings-group__heading">
          <h2 className="settings-group__title" id="settings-credits-title">Credits wallet</h2>
          <p className="settings-group__desc">Pays for apps that charge per use.</p>
        </div>
      </div>
      <div className="settings-list">
        <div className="settings-item">
          <span className="settings-item__body">
            <span className="settings-item__title">Not connected</span>
            <span className="settings-item__sub settings-item__sub--wrap">
              Connect this Möbius to your mobius.you credits to buy credits and use
              apps that charge per use.
            </span>
          </span>
          <span className="settings-item__trail">
            <ConnectButton busy={busy} label="Connect credits" onClick={onConnect} />
          </span>
        </div>
      </div>
      {refusal && <p className="settings-message settings-message--error" role="alert">{refusal}</p>}
    </section>
  )
}

function ConnectButton({ busy, label, onClick }) {
  return (
    <button type="button" className="settings__btn settings__btn--sm" disabled={busy} aria-busy={busy} onClick={onClick}>
      {busy ? 'Connecting…' : label}
    </button>
  )
}

export function WalletBuyForm({ terms, busy, onBuy, onCancel }) {
  const amounts = terms.amounts_cents
  const [cents, setCents] = useState(amounts[0])
  const [waiver, setWaiver] = useState(false)
  const chosen = amounts.includes(cents) ? cents : amounts[0]

  const submit = event => {
    event.preventDefault()
    onBuy({ amountCents: chosen, waiverAccepted: waiver })
  }

  return (
    <form className="settings-item-panel" id="settings-credits-buy" onSubmit={submit}>
      <div className="settings-actions credits__amounts-row" role="radiogroup" aria-label="Amount">
        {amounts.map(value => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={value === chosen}
            className={`settings__btn settings__btn--outline settings__btn--sm credits__amount-choice${value === chosen ? ' credits__amount-choice--on' : ''}`}
            onClick={() => setCents(value)}
          >
            {money(value, terms.currency)}
          </button>
        ))}
      </div>
      <p className="settings-item-panel__text">
        1 credit = {money(100, terms.currency)}. Paid securely through Stripe
        {terms.stripe_mode === 'test' ? ' (test mode — no real money moves)' : ''}.
      </p>
      {terms.waiver_required && (
        <label className="credits__waiver settings-item-panel__text">
          <input
            name="waiver"
            type="checkbox"
            checked={waiver}
            onChange={event => setWaiver(event.target.checked)}
          />
          <span>{WAIVER}</span>
        </label>
      )}
      <div className="settings-actions">
        <button type="submit" className="settings__btn settings__btn--sm" disabled={busy} aria-busy={busy}>
          {busy ? 'Finish in the Stripe window…' : `Buy ${money(chosen, terms.currency)} of credits`}
        </button>
        {!busy && (
          <button type="button" className="settings__btn settings__btn--outline settings__btn--sm" onClick={onCancel}>
            Cancel
          </button>
        )}
      </div>
    </form>
  )
}
