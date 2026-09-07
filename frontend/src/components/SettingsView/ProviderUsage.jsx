/* Compact plan and allowance detail rendered under a connected provider row. */

import { useEffect, useState } from 'react'

import {
  bankedResetCredits,
  clampUsagePercent,
  formatResetExpiry,
  formatUsagePercent,
  formatUsageReset,
  redeemOutcomeMessage,
  soonestResetExpiry,
  visibleUsageWindows,
} from './providerUsage.js'

function BankedResets({ resets, onRedeem, redeeming = false, result = null }) {
  const [confirming, setConfirming] = useState(false)
  // A finished redeem (success or handled error) always leaves the confirm step.
  useEffect(() => {
    if (result) setConfirming(false)
  }, [result])
  if (!resets) return null

  const expiry = soonestResetExpiry(resets.credits)
  const expiryLabel = expiry ? formatResetExpiry(expiry) : ''
  const message = result
    ? (result.error ? redeemOutcomeMessage(undefined) : redeemOutcomeMessage(result.outcome))
    : null
  const count = resets.availableCount
  const countLabel = count === 1 ? '1 banked reset' : `${count} banked resets`

  return (
    <span className="provider-usage__resets">
      <span className="provider-usage__resets-head">
        <span className="provider-usage__resets-count">{countLabel}</span>
        {expiryLabel && (
          <span className="provider-usage__resets-expiry">{expiryLabel}</span>
        )}
      </span>
      {confirming ? (
        <span className="provider-usage__resets-confirm">
          <span className="provider-usage__resets-warn">
            Spends one saved reset and clears your usage now. This can’t be undone.
          </span>
          <span className="provider-usage__resets-actions">
            <button
              type="button"
              className="provider-usage__redeem provider-usage__redeem--go"
              disabled={redeeming}
              onClick={() => onRedeem()}
            >
              {redeeming ? 'Redeeming…' : 'Confirm reset'}
            </button>
            <button
              type="button"
              className="provider-usage__redeem provider-usage__redeem--cancel"
              disabled={redeeming}
              onClick={() => setConfirming(false)}
            >
              Cancel
            </button>
          </span>
        </span>
      ) : (
        <button
          type="button"
          className="provider-usage__redeem"
          disabled={redeeming}
          onClick={() => setConfirming(true)}
        >
          Use a reset
        </button>
      )}
      {message && (
        <span
          className={`provider-usage__resets-msg provider-usage__resets-msg--${message.tone}`}
          role="status"
        >
          {message.text}
        </span>
      )}
    </span>
  )
}

export default function ProviderUsage({
  id,
  snapshot,
  loading = false,
  failed = false,
  onRedeemReset = null,
  redeeming = false,
  redeemResult = null,
}) {
  if (loading && !snapshot) {
    return (
      <span id={id} className="provider-usage provider-usage--message" role="status">
        Checking plan usage…
      </span>
    )
  }

  if (!snapshot && !failed) return null
  const windows = visibleUsageWindows(snapshot)
  const ready = snapshot?.state === 'ready' && windows.length > 0

  return (
    <span id={id} className="provider-usage">
      {ready ? (
        <span className="provider-usage__windows">
          {windows.map(window => {
            const percent = clampUsagePercent(window.used_percent)
            const reset = formatUsageReset(window.resets_at)
            return (
              <span className="provider-usage__window" key={window.id || window.label}>
                <span className="provider-usage__label">{window.label}</span>
                <span
                  className="provider-usage__track"
                  role="progressbar"
                  aria-label={`${window.label} usage`}
                  aria-valuemin="0"
                  aria-valuemax="100"
                  aria-valuenow={percent}
                >
                  <span
                    className="provider-usage__fill"
                    style={{ width: `${percent}%` }}
                  />
                </span>
                <span className="provider-usage__value">
                  {formatUsagePercent(percent)}%
                </span>
                {reset && <span className="provider-usage__reset">{reset}</span>}
              </span>
            )
          })}
          {snapshot?.credit_balance && (
            <span className="provider-usage__credit">{snapshot.credit_balance}</span>
          )}
          {snapshot?.stale && (
            <span className="provider-usage__credit">Last available reading</span>
          )}
          {onRedeemReset && (
            <BankedResets
              resets={bankedResetCredits(snapshot)}
              onRedeem={onRedeemReset}
              redeeming={redeeming}
              result={redeemResult}
            />
          )}
        </span>
      ) : (
        <span className="provider-usage__unavailable">Usage unavailable</span>
      )}
    </span>
  )
}
