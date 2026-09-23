/* Compact plan and allowance detail rendered under a connected provider row. */

import { useEffect, useState } from 'react'

import {
  bankedResetCredits,
  claudeRedeemOutcomeMessage,
  claudeResetCredits,
  clampUsagePercent,
  formatResetExpiry,
  formatUsagePercent,
  formatUsageReset,
  redeemOutcomeMessage,
  soonestResetExpiry,
  visibleUsageWindows,
} from './providerUsage.js'

function BankedResets({
  resets,
  onRedeem,
  redeeming = false,
  result = null,
  provider = 'codex',
}) {
  const [confirming, setConfirming] = useState(false)
  // A finished redeem (success or handled error) always leaves the confirm step.
  useEffect(() => {
    if (result) setConfirming(false)
  }, [result])
  if (!resets) return null

  const expiry = soonestResetExpiry(resets.credits)
  const count = resets.availableCount
  const claude = provider === 'claude'
  const detail = claude && !resets.eligible
    ? 'Limit resets aren’t available for this Claude connection'
    : [
        count === 1 ? '1 banked reset' : `${count} banked resets`,
        expiry ? formatResetExpiry(expiry) : '',
        claude && count > 0 && !resets.redeemable
          ? 'available when Claude offers it for the current limit'
          : '',
      ].filter(Boolean).join(' · ')
  const message = result
    ? (claude ? claudeRedeemOutcomeMessage : redeemOutcomeMessage)(
        result.error ? undefined : result.outcome,
      )
    : null
  const redeemDisabled = count === 0 || (claude && (!resets.redeemable || result?.error))

  return (
    <span className="provider-usage__resets">
      <span className="provider-usage__resets-row">
        <span className="provider-usage__resets-detail">
          {confirming
            ? `Spend one reset and clear ${claude ? 'eligible Claude limits' : 'usage'} now?`
            : detail}
        </span>
        {confirming ? (
          <span className="provider-usage__resets-actions">
            <button
              type="button"
              className="provider-usage__redeem"
              disabled={redeeming}
              onClick={() => onRedeem(resets.nextCreditId || null)}
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
            disabled={redeemDisabled}
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

function ClaudeExtraUsage({ extra, onToggle, busy = false, result = null }) {
  const [confirming, setConfirming] = useState(null)
  useEffect(() => {
    if (result) setConfirming(null)
  }, [result])
  if (!extra || extra.manageable !== true || !onToggle) return null
  const enabled = extra.enabled === true
  const next = !enabled
  const message = result?.error
    ? 'Claude could not change extra usage. Manage it on claude.ai.'
    : result
      ? `Extra usage ${result.enabled ? 'enabled' : 'disabled'}.`
      : null

  return (
    <span className="provider-usage__resets">
      <span className="provider-usage__resets-row">
        <span className="provider-usage__resets-detail">
          {confirming === next
            ? `${next ? 'Enable' : 'Disable'} paid extra usage?`
            : `Extra usage ${enabled ? 'enabled' : 'disabled'}`}
        </span>
        {confirming === next ? (
          <span className="provider-usage__resets-actions">
            <button
              type="button"
              className="provider-usage__redeem"
              disabled={busy}
              onClick={() => onToggle(next, enabled)}
            >
              {busy ? 'Saving…' : 'Confirm'}
            </button>
            <button
              type="button"
              className="provider-usage__redeem provider-usage__redeem--ghost"
              disabled={busy}
              onClick={() => setConfirming(null)}
            >
              Cancel
            </button>
          </span>
        ) : (
          <button
            type="button"
            className="provider-usage__redeem"
            onClick={() => setConfirming(next)}
          >
            {enabled ? 'Turn off' : 'Turn on'}
          </button>
        )}
      </span>
      {message && (
        <span
          className={`provider-usage__resets-msg provider-usage__resets-msg--${result?.error ? 'error' : 'success'}`}
          role="status"
        >
          {message}
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
  onRedeemClaudeReset = null,
  claudeResetRedeeming = false,
  claudeResetResult = null,
  onToggleExtraUsage = null,
  extraUsageBusy = false,
  extraUsageResult = null,
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
  const claudeResets = onRedeemClaudeReset ? claudeResetCredits(snapshot) : null

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
      {claudeResets && (
        <BankedResets
          resets={claudeResets}
          onRedeem={onRedeemClaudeReset}
          redeeming={claudeResetRedeeming}
          result={claudeResetResult}
          provider="claude"
        />
      )}
      <ClaudeExtraUsage
        extra={snapshot?.extra_usage}
        onToggle={onToggleExtraUsage}
        busy={extraUsageBusy}
        result={extraUsageResult}
      />
    </span>
  )
}
