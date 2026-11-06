/** The credits service's wallet for app purchases, distinct from model credit. */
import { useState } from 'react'
import { activityLabel, money } from '../../lib/creditsWallet.js'
import './WalletSection.css'

const WAIVER = 'I want my credits straight away, and I understand that I lose my right '
  + 'to cancel once they have been delivered.'

function when(at) {
  const date = new Date(at)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString(undefined, {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  })
}

export default function WalletSection({
  summary, terms, activity, busy = false, notice = null, refusal = null,
  onBuy = () => {},
}) {
  const balance = summary?.balance
  const owed = typeof balance?.balance === 'string' && balance.balance.startsWith('-')
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
          <div className="settings-item__body">
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
          </div>
          {owed && <span className="settings-item__trail"><span className="settings-badge settings-badge--warning">Owed</span></span>}
        </div>
        {balance && terms?.amounts_cents?.length ? (
          // A choice or waiver for one service never carries over to another.
          <WalletBuyForm key={terms.pairing} terms={terms} busy={busy} onBuy={onBuy} />
        ) : null}
      </div>
      {notices[notice] && <p className="settings-message" role="status">{notices[notice]}</p>}
      {refusal && <p className="settings-message settings-message--error" role="alert">{refusal}</p>}

      {activity?.length ? (
        <section className="settings-group" aria-labelledby="settings-credits-activity">
          <div className="settings-group__head">
            <h3 className="settings-group__title" id="settings-credits-activity">Recent activity</h3>
          </div>
          <div className="settings-list" role="list">
            {activity.map(entry => (
              <div className="settings-item" role="listitem" key={entry.id}>
                <span className="settings-item__body">
                  <span className="settings-item__title">{activityLabel(entry)}</span>
                  <span className="settings-item__sub">{when(entry.at)}</span>
                </span>
                <span className={`settings-item__trail credits__activity-amount${String(entry.amount).startsWith('-') ? '' : ' credits__activity-amount--in'}`}>
                  {String(entry.amount).startsWith('-') ? entry.amount : `+${entry.amount}`}
                </span>
              </div>
            ))}
          </div>
        </section>
      ) : null}
    </section>
  )
}

/** Credits for a Möbius launched from mobius.you that is not connected yet. */
export function WalletConnect({ busy = false, refusal = null, onConnect = () => {} }) {
  return (
    <section className="settings__section credits">
      <h2 className="settings__section-title">Credits</h2>
      <p className="credits__quiet">
        Connect this Möbius to your mobius.you credits to buy credits and use
        apps that charge per use.
      </p>
      {refusal && <p className="credits__refusal" role="alert">{refusal}</p>}
      <button type="button" className="credits__buy" disabled={busy} aria-busy={busy} onClick={onConnect}>
        {busy ? 'Connecting…' : 'Connect credits'}
      </button>
    </section>
  )
}

function WalletBuyForm({ terms, busy, onBuy }) {
  const amounts = terms.amounts_cents
  const [cents, setCents] = useState(amounts[0])
  const [waiver, setWaiver] = useState(false)
  const chosen = amounts.includes(cents) ? cents : amounts[0]

  const submit = event => {
    event.preventDefault()
    onBuy({ amountCents: chosen, waiverAccepted: waiver })
  }

  return (
    <form className="settings-item settings-item--block" onSubmit={submit}>
      <div className="settings-actions" role="radiogroup" aria-label="Amount">
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
      <p className="settings-group__foot">
        1 credit = {money(100, terms.currency)}. Paid securely through Stripe
        {terms.stripe_mode === 'test' ? ' (test mode — no real money moves)' : ''}.
      </p>
      {terms.waiver_required && (
        <label className="credits__waiver settings-group__foot">
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
      </div>
    </form>
  )
}
