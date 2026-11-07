/* Provider plan usage: a compact meter for the Settings overview row and the Usage section of a provider page. */

import { useEffect, useState } from 'react'

import {
  bankedResetCredits,
  claudeRedeemOutcomeMessage,
  claudeResetCredits,
  clampUsagePercent,
  formatCreditBalance,
  formatResetExpiry,
  formatUsagePercent,
  formatUsageObservedAt,
  formatUsageReset,
  redeemOutcomeMessage,
  soonestResetExpiry,
  usageTone,
  visibleUsageWindows,
} from './providerUsage.js'

/** The one usage figure a provider row shows at the top level of Settings. */
export function UsageMeter({ usage }) {
  if (!usage) return null
  const percent = clampUsagePercent(usage.percent)
  const tone = usageTone(percent)
  return (
    <span
      className={`settings-meter settings-meter--compact${tone ? ` settings-meter--${tone}` : ''}`}
      role="img"
      aria-label={usage.summary}
      title={usage.summary}
    >
      <span className="settings-meter__track"><span className="settings-meter__fill" style={{ width: `${percent}%` }} /></span>
      <span className="settings-meter__label">
        <span>{usage.label}</span>
        <span className="settings-meter__value">{formatUsagePercent(percent)}%</span>
      </span>
    </span>
  )
}

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
  // A Claude connection that can never redeem has nothing to show or do.
  if (provider === 'claude' && !resets.eligible && resets.availableCount === 0 && !result) return null

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
  const redeemDisabled = count === 0 || (claude && !resets.redeemable)

  return (
    <div className="settings-item settings-item--top provider-usage__action-row">
      <span className="settings-item__body">
        <span className="settings-item__title">Limit resets</span>
        <span className="settings-item__sub settings-item__sub--wrap">
          {confirming
            ? `Spend one reset and clear ${claude ? 'eligible Claude limits' : 'usage'} now?`
            : detail}
        </span>
        {message && (
          <span
            className={`settings-message settings-message--${message.tone === 'success' ? 'success' : message.tone === 'error' ? 'error' : 'info'} provider-usage__msg`}
            role="status"
          >
            {message.text}
          </span>
        )}
      </span>
      {confirming ? (
        <span className="settings-item__trail">
          <button
            type="button"
            className="settings__btn settings__btn--sm settings__btn--outline"
            disabled={redeeming}
            onClick={() => setConfirming(false)}
          >
            Cancel
          </button>
          <button
            type="button"
            className="settings__btn settings__btn--sm"
            disabled={redeeming}
            onClick={() => onRedeem(
              resets.nextCreditId || null,
              resets.nextCreditResetsLeft || null,
            )}
          >
            {redeeming ? 'Redeeming…' : 'Confirm'}
          </button>
        </span>
      ) : (
        <span className="settings-item__trail">
          <button
            type="button"
            className="settings__btn settings__btn--sm settings__btn--outline"
            disabled={redeemDisabled}
            onClick={() => setConfirming(true)}
          >
            Use a reset
          </button>
        </span>
      )}
    </div>
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
    <div className="settings-item settings-item--top provider-usage__action-row">
      <span className="settings-item__body">
        <span className="settings-item__title">Extra usage</span>
        <span className="settings-item__sub settings-item__sub--wrap">
          {confirming === next
            ? `${next ? 'Enable' : 'Disable'} paid extra usage?`
            : `Paid usage beyond your plan · ${enabled ? 'On' : 'Off'}`}
        </span>
        {message && (
          <span
            className={`settings-message settings-message--${result?.error ? 'error' : 'success'} provider-usage__msg`}
            role="status"
          >
            {message}
          </span>
        )}
      </span>
      {confirming === next ? (
        <span className="settings-item__trail">
          <button
            type="button"
            className="settings__btn settings__btn--sm settings__btn--outline"
            disabled={busy}
            onClick={() => setConfirming(null)}
          >
            Cancel
          </button>
          <button
            type="button"
            className="settings__btn settings__btn--sm"
            disabled={busy}
            onClick={() => onToggle(next, enabled)}
          >
            {busy ? 'Saving…' : 'Confirm'}
          </button>
        </span>
      ) : (
        <span className="settings-item__trail">
          <button
            type="button"
            className="settings__btn settings__btn--sm settings__btn--outline"
            onClick={() => setConfirming(next)}
          >
            {enabled ? 'Turn off' : 'Turn on'}
          </button>
        </span>
      )}
    </div>
  )
}

/** The Usage section of a provider page: each allowance window, then account actions. */
export default function ProviderUsage({
  id,
  snapshot,
  loading = false,
  failed = false,
  refreshing = false,
  onRefresh = null,
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
  const checkedAt = formatUsageObservedAt(snapshot?.observed_at)
  const freshnessLabel = loading && !snapshot
    ? 'Checking…'
    : checkedAt
      ? `${failed ? 'Could not refresh · ' : snapshot?.stale ? 'Last available reading · ' : ''}${checkedAt}`
      : failed ? 'Could not refresh' : 'Time unavailable'
  // Paid extra usage is its own row with its own switch; while it is off its
  // 0% window would only restate that.
  const extraOff = snapshot?.extra_usage && snapshot.extra_usage.enabled !== true
  const windows = visibleUsageWindows(snapshot)
    .filter(window => !(extraOff && window.id === 'extra_usage'))
  const ready = snapshot?.state === 'ready' && windows.length > 0
  const bankedResets = onRedeemReset ? bankedResetCredits(snapshot) : null
  const claudeResets = onRedeemClaudeReset ? claudeResetCredits(snapshot) : null
  const credit = formatCreditBalance(snapshot?.credit_balance)

  return (
    <section id={id} className="settings-group provider-usage" aria-labelledby={`${id}-title`}>
      <div className="settings-group__head">
        <div className="settings-group__heading">
          <h2 className="settings-group__title" id={`${id}-title`}>Usage</h2>
        </div>
        <div className="settings-group__aside">
          <span>{freshnessLabel}</span>
          {onRefresh && <button type="button" className="settings__btn settings__btn--sm settings__btn--outline provider-usage__refresh" onClick={onRefresh} disabled={refreshing || (loading && !snapshot)}>{refreshing ? 'Refreshing…' : 'Refresh'}</button>}
        </div>
      </div>
      <div className="settings-list">
        {ready ? windows.map(window => {
          const percent = clampUsagePercent(window.used_percent)
          const tone = usageTone(percent)
          const reset = formatUsageReset(window.resets_at)
          return (
            <div className="settings-item settings-item--block" key={window.id || window.label}>
              <div className={`settings-meter provider-usage__window${tone ? ` settings-meter--${tone}` : ''}`}>
                <div className="provider-usage__window-head">
                  <span className="settings-item__title">{window.label}</span>
                  <span className="provider-usage__percent">{formatUsagePercent(percent)}%</span>
                </div>
                <span
                  className="settings-meter__track"
                  role="progressbar"
                  aria-label={`${window.label} usage`}
                  aria-valuemin="0"
                  aria-valuemax="100"
                  aria-valuenow={percent}
                >
                  <span className="settings-meter__fill" style={{ width: `${percent}%` }} />
                </span>
                {reset && <span className="settings-item__sub">{reset}</span>}
              </div>
            </div>
          )
        }) : (
          <div className="settings-item">
            <span className="settings-item__body">
              <span className="settings-item__sub">{loading && !snapshot ? 'Checking plan usage…' : 'Usage unavailable'}</span>
            </span>
          </div>
        )}
        {credit && (
          <div className="settings-item">
            <span className="settings-item__body"><span className="settings-item__title">Credits</span></span>
            <span className="settings-item__value">{credit}</span>
          </div>
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
      </div>
    </section>
  )
}
