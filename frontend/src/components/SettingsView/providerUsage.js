/* Pure display helpers for compact provider-plan usage snapshots. */

export function formatPlanStatus(label) {
  const value = typeof label === 'string' ? label.trim() : ''
  const name = value.replace(/\s+plan$/i, '').trim()
  return `Plan: ${name || 'Unknown'}`
}

export function clampUsagePercent(value) {
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return 0
  return Math.min(100, Math.max(0, numeric))
}

export function formatUsagePercent(value) {
  const numeric = clampUsagePercent(value)
  return Number.isInteger(numeric) ? String(numeric) : numeric.toFixed(1)
}

export function formatTrialTimeLeft(value, now = new Date()) {
  if (!value) return ''
  const expiry = new Date(value)
  if (Number.isNaN(expiry.getTime())) return ''
  const remainingMs = expiry.getTime() - now.getTime()
  if (remainingMs <= 0) return 'Ended'
  return `${Math.ceil(remainingMs / 86_400_000)}d left`
}

export function formatUsageReset(value, now = new Date()) {
  if (!value) return ''
  const reset = new Date(value)
  if (Number.isNaN(reset.getTime())) return ''
  const sameDay = (
    reset.getFullYear() === now.getFullYear()
    && reset.getMonth() === now.getMonth()
    && reset.getDate() === now.getDate()
  )
  const time = new Intl.DateTimeFormat(undefined, {
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).format(reset)
  if (sameDay) return `Resets ${time}`
  const day = new Intl.DateTimeFormat(undefined, { weekday: 'short' }).format(reset)
  return `Resets ${day} ${time}`
}

export function visibleUsageWindows(snapshot) {
  if (!Array.isArray(snapshot?.windows)) return []
  return snapshot.windows
    .filter(window => window && typeof window.label === 'string')
    .slice(0, 4)
}

export function providerAllowance(provider, snapshot) {
  const kind = provider === 'mobius' ? 'api_credits' : 'weekly'
  const label = kind === 'api_credits' ? 'API credits usage' : 'Weekly usage'
  if (snapshot?.state !== 'ready' || !Array.isArray(snapshot.windows)) {
    return { kind, label, usedPercent: null, expiresAt: null }
  }
  const window = snapshot.windows.find(candidate => candidate?.kind === kind)
  const used = window?.used_percent == null ? Number.NaN : Number(window.used_percent)
  return {
    kind,
    label,
    usedPercent: Number.isFinite(used) ? clampUsagePercent(used) : null,
    expiresAt: typeof window?.expires_at === 'string' ? window.expires_at : null,
  }
}

export function providerAllowanceSummary(provider, allowance, now = new Date()) {
  if (provider === 'mobius' && typeof allowance?.usedPercent === 'number') {
    return [
      `${Math.round(allowance.usedPercent)}% used`,
      formatTrialTimeLeft(allowance.expiresAt, now),
    ].filter(Boolean).join(' · ')
  }
  if (typeof allowance?.usedPercent === 'number') {
    return `${Math.round(allowance.usedPercent)}% ${allowance.label.toLowerCase()}`
  }
  return allowance?.label || 'Usage'
}
