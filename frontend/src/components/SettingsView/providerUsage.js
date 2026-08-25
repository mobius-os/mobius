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

export function providerAllowance(snapshot) {
  const kind = 'weekly'
  const label = 'Weekly usage'
  if (snapshot?.state !== 'ready' || !Array.isArray(snapshot.windows)) {
    return { kind, label, usedPercent: null }
  }
  const window = snapshot.windows.find(candidate => candidate?.kind === kind)
  const used = Number(window?.used_percent)
  return {
    kind,
    label,
    usedPercent: Number.isFinite(used) ? clampUsagePercent(used) : null,
  }
}
