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
  const count = resets.availableCount
  const detail = [
    count === 1 ? '1 banked reset' : `${count} banked resets`,
    expiry ? formatResetExpiry(expiry) : '',
  ].filter(Boolean).join(' · ')
  const message = result
    ? redeemOutcomeMessage(result.error ? undefined : result.outcome)
    : null

  return (
    <span className="provider-usage__resets">
      <span className="provider-usage__resets-row">
        <span className="provider-usage__resets-detail">
          {confirming ? 'Spend one reset and clear usage now?' : detail}
        </span>
        {confirming ? (
          <span className="provider-usage__resets-actions">
            <button
              type="button"
              className="provider-usage__redeem"
              disabled={redeeming}
              onClick={() => onRedeem()}
            >
              {redeeming ? 'Redeeming…' : 'Confirm'}
            </button>
            <button
              type="button"
              className="provider-usage__redeem provider-usage__redeem--ghost"
              disabled={redeeming}
              onClick={() => setConfirming(false)}
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="provider-usage__redeem"
            disabled={count === 0}
            onClick={() => setConfirming(true)}
          >
            Use a reset
          </button>
        )}
      </span>
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
  const bankedResets = onRedeemReset ? bankedResetCredits(snapshot) : null

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
        </span>
      ) : (
        <span className="provider-usage__unavailable">Usage unavailable</span>
      )}
      {bankedResets && (
        <BankedResets
          resets={bankedResets}
          onRedeem={onRedeemReset}
          redeeming={redeeming}
          result={redeemResult}
        />
      )}
    </span>
  )
}
