/* Compact plan and allowance detail rendered under a connected provider row. */

import {
  clampUsagePercent,
  formatUsagePercent,
  formatUsageReset,
  visibleUsageWindows,
} from './providerUsage.js'

export default function ProviderUsage({
  id,
  snapshot,
  loading = false,
  failed = false,
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
        </span>
      ) : (
        <span className="provider-usage__unavailable">Usage unavailable</span>
      )}
    </span>
  )
}
