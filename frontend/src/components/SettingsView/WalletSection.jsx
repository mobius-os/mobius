/**
 * Credits for a Möbius paired with a credits service: the wallet apps spend from.
 *
 * Presentational: the wallet's balance, activity and purchase terms arrive
 * from `useWallet`, and the rules worth testing live in lib/creditsWallet.js.
 * Money is shown as the service rendered it, never recomputed here.
 */

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

  return (
    <section className="settings__section credits">
      <h2 className="settings__section-title">Credits</h2>

      {balance ? (
        <div className="credits__headline">
          {owed ? (
            <>
              <span className="credits__owed">You owe {balance.balance.slice(1)}</span>
              <span className="credits__unit">credits</span>
            </>
          ) : (
            <>
              <span className="credits__amount">{balance.available}</span>
              <span className="credits__unit">credits available</span>
            </>
          )}
          <span className="credits__forever">
            Kept by {summary.service_host}. Apps that charge per use spend from
            this balance. Credits never expire.
          </span>
        </div>
      ) : (
        <p className="credits__quiet">
          {summary?.error?.message
            ? `Your balance could not be read just now: ${summary.error.message}`
            : 'Your balance could not be read just now. Try again in a moment.'}
        </p>
      )}

      {notice === 'checking' && (
        <p className="credits__settling">Checking your payment…</p>
      )}
      {notice === 'paid' && (
        <p className="credits__settling">Payment received. Your credits are in.</p>
      )}
      {notice === 'late' && (
        <p className="credits__settling">
          Stripe has not confirmed that payment yet. If you paid, your credits
          appear within a minute or two — please don't buy again.
        </p>
      )}
      {refusal && <p className="credits__refusal" role="alert">{refusal}</p>}

      {balance && terms?.amounts_cents?.length ? (
        // Keyed by pairing: a choice or waiver made for one service's terms
        // never carries over to another's.
        <WalletBuyForm key={terms.pairing} terms={terms} busy={busy} onBuy={onBuy} />
      ) : null}

      {activity?.length ? (
        <div className="credits__activity">
          <h3 className="credits__activity-title">Recent activity</h3>
          <ul>
            {activity.map(entry => (
              <li key={entry.id}>
                <span className="credits__activity-what">{activityLabel(entry)}</span>
                <span className="credits__activity-when">{when(entry.at)}</span>
                <span className={`credits__activity-amount${String(entry.amount).startsWith('-') ? '' : ' credits__activity-amount--in'}`}>
                  {String(entry.amount).startsWith('-') ? entry.amount : `+${entry.amount}`}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : null}

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
    <form className="credits__buy-form" onSubmit={submit}>
      <div className="credits__amounts" role="radiogroup" aria-label="Amount">
        {amounts.map(value => (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={value === chosen}
            className={`credits__amount-choice${value === chosen ? ' credits__amount-choice--on' : ''}`}
            onClick={() => setCents(value)}
          >
            {money(value, terms.currency)}
          </button>
        ))}
      </div>

      {terms.waiver_required && (
        <label className="credits__waiver">
          <input
            name="waiver"
            type="checkbox"
            checked={waiver}
            onChange={event => setWaiver(event.target.checked)}
          />
          <span>{WAIVER}</span>
        </label>
      )}

      <button type="submit" className="credits__buy" disabled={busy} aria-busy={busy}>
        {busy ? 'Finish in the Stripe window…' : `Buy ${money(chosen, terms.currency)} of credits`}
      </button>

      <p className="credits__terms">
        1 credit = {money(100, terms.currency)}. Paid securely through Stripe
        {terms.stripe_mode === 'test' ? ' (test mode — no real money moves)' : ''}.
      </p>
    </form>
  )
}
