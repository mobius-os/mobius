/* Pure display helpers for compact provider-plan usage snapshots. */
import { formatDateTime } from '../../lib/dateTimeFormat.js'

/** A plan label as a page subtitle: "Max plan" → "Max plan", "Pro" → "Pro plan"; empty stays empty. */
export function formatPlanName(label) {
  const value = typeof label === 'string' ? label.trim() : ''
  const name = value.replace(/\s+plan$/i, '').trim()
  if (!name) return ''
  // Codex's account protocol exposes this internal tier name rather than a
  // customer-facing plan label. Keep the verified Business family, not the
  // implementation suffix.
  if (/^Self Serve Business Prolite$/i.test(name)) return 'Business plan'
  return `${name} plan`
}

export function formatUsageObservedAt(value) {
  if (typeof value !== 'string' || !value) return ''
  const checked = new Date(value)
  return Number.isNaN(checked.getTime()) ? '' : `Checked ${formatDateTime(checked)}`
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

export function formatUsageReset(value) {
  if (!value) return ''
  const reset = new Date(value)
  if (Number.isNaN(reset.getTime())) return ''
  return `Resets ${formatDateTime(reset)}`
}

export function bankedResetCredits(snapshot) {
  const summary = snapshot?.reset_credits
  if (!snapshot) return null
  if (!summary || typeof summary !== 'object') return { availableCount: 0, credits: [] }
  const count = Number(summary?.available_count)
  if (!Number.isFinite(count) || count < 0) return null
  const credits = Array.isArray(summary.credits) ? summary.credits : []
  return { availableCount: count, credits }
}

export function claudeResetCredits(snapshot) {
  const summary = snapshot?.reset_credits
  if (!summary || typeof summary !== 'object') return null
  const count = Number(summary.available_count)
  if (!Number.isFinite(count) || count < 0) return null
  const credits = Array.isArray(summary.credits) ? summary.credits : []
  const nextCreditId = typeof summary.next_credit_id === 'string'
    ? summary.next_credit_id
    : null
  const nextCredit = credits.find(credit => credit?.id === nextCreditId)
  const nextCreditResetsLeft = Number(nextCredit?.resets_left)
  return {
    availableCount: count,
    credits,
    eligible: summary.eligible === true,
    redeemable: summary.redeemable === true,
    nextCreditId,
    nextCreditResetsLeft: Number.isInteger(nextCreditResetsLeft)
      && nextCreditResetsLeft > 0
      ? nextCreditResetsLeft
      : null,
  }
}

export function soonestResetExpiry(credits) {
  if (!Array.isArray(credits)) return null
  const times = credits
    .map(credit => credit?.expires_at)
    .filter(value => typeof value === 'string')
    .map(value => new Date(value))
    .filter(date => !Number.isNaN(date.getTime()))
  if (times.length === 0) return null
  return new Date(Math.min(...times.map(date => date.getTime())))
}

export function formatResetExpiry(expiry, now = new Date()) {
  const date = expiry instanceof Date ? expiry : new Date(expiry)
  if (Number.isNaN(date.getTime())) return ''
  const remainingMs = date.getTime() - now.getTime()
  if (remainingMs <= 0) return 'Expired'
  const days = Math.ceil(remainingMs / 86_400_000)
  if (days <= 1) return 'Expires within a day'
  return `Expires in ${days}d`
}

export function redeemOutcomeMessage(outcome) {
  switch (outcome) {
    case 'reset':
      return { tone: 'success', text: 'Reset applied — your usage window is clear.' }
    case 'nothingToReset':
      return { tone: 'info', text: 'Nothing to reset — usage was already clear.' }
    case 'noCredit':
      return { tone: 'info', text: 'No banked resets are available right now.' }
    case 'alreadyRedeemed':
      return { tone: 'info', text: 'That reset was already redeemed.' }
    default:
      return { tone: 'error', text: 'Couldn’t redeem the reset. Try again shortly.' }
  }
}

export function claudeRedeemOutcomeMessage(outcome) {
  switch (outcome) {
    case 'reset':
      return { tone: 'success', text: 'Reset applied — Claude is checking your refreshed limits.' }
    case 'already_used':
      return { tone: 'info', text: 'That reset was already used — nothing else was spent.' }
    case 'not_limited':
      return { tone: 'info', text: 'Your limits are already clear — no reset was spent.' }
    case 'cooldown':
      return { tone: 'info', text: 'A Claude reset is already being applied.' }
    case 'ineligible':
    case 'unavailable':
    case 'offer_changed':
      return { tone: 'info', text: 'That reset is no longer available.' }
    case 'unknown':
      return {
        tone: 'info',
        text: 'Claude may have applied that reset. Möbius will reconcile it before another reset.',
      }
    default:
      return { tone: 'error', text: 'Couldn’t redeem the Claude reset. Try again shortly.' }
  }
}

export function visibleUsageWindows(snapshot) {
  if (!Array.isArray(snapshot?.windows)) return []
  return snapshot.windows
    .filter(window => window && typeof window.label === 'string')
    .slice(0, 4)
}

export function providerExtraUsage(snapshot) {
  const extra = snapshot?.extra_usage
  if (extra?.enabled !== true) return null
  const used = extra.used_percent
  if (!Number.isFinite(used)) {
    return { label: 'Extra usage', usedPercent: null, summary: 'Extra usage enabled' }
  }
  const usedPercent = clampUsagePercent(used)
  return {
    label: 'Extra usage',
    usedPercent,
    summary: `${formatUsagePercent(usedPercent)}% extra usage used`,
  }
}

const ALLOWANCE_LABELS = {
  api_credits: 'API credits usage',
  five_hour: '5-hour usage',
  weekly: 'Weekly usage',
}

function hasWindowKind(snapshot, kind) {
  return snapshot?.state === 'ready'
    && Array.isArray(snapshot.windows)
    && snapshot.windows.some(candidate => candidate?.kind === kind)
}

// Plan providers gauge the 5-hour window: it usually blocks first. Weekly is
// gauged only when the provider reports no 5-hour window at all.
function gaugeKind(provider, snapshot) {
  if (provider === 'mobius') return 'api_credits'
  if (!hasWindowKind(snapshot, 'five_hour') && hasWindowKind(snapshot, 'weekly')) {
    return 'weekly'
  }
  return 'five_hour'
}

// A reading taken before its window reset says nothing about that window now.
// The server never serves one, but the last reading restored from disk can be
// one while the server is unreachable, so such a window reads as unknown.
function currentUsedPercent(window, now) {
  if (window?.used_percent == null) return null
  const used = Number(window.used_percent)
  if (!Number.isFinite(used)) return null
  const reset = window.resets_at ? new Date(window.resets_at) : null
  if (reset && !Number.isNaN(reset.getTime()) && reset <= now) return null
  return clampUsagePercent(used)
}

function allowanceForKind(provider, snapshot, kind, now) {
  const fallbackLabel = ALLOWANCE_LABELS[kind]
  if (snapshot?.state !== 'ready' || !Array.isArray(snapshot.windows)) {
    return {
      kind,
      label: fallbackLabel,
      usedPercent: null,
      expiresAt: null,
    }
  }
  const window = snapshot.windows.find(candidate => candidate?.kind === kind)
  const label = provider === 'codex' && kind === 'weekly' && window?.label === '7-day'
    ? '7-day usage'
    : fallbackLabel
  return {
    kind,
    label,
    usedPercent: currentUsedPercent(window, now),
    expiresAt: typeof window?.expires_at === 'string' ? window.expires_at : null,
    ...(snapshot.stale === true ? { stale: true } : {}),
  }
}

/** The allowance the brain's left lobe fills with. */
export function providerAllowance(provider, snapshot, now = new Date()) {
  return allowanceForKind(provider, snapshot, gaugeKind(provider, snapshot), now)
}

/** The weekly allowance listed beside a 5-hour gauge, when one is current. */
export function providerWeeklyAllowance(provider, snapshot, now = new Date()) {
  if (gaugeKind(provider, snapshot) !== 'five_hour') return null
  if (!hasWindowKind(snapshot, 'weekly')) return null
  const weekly = allowanceForKind(provider, snapshot, 'weekly', now)
  return weekly.usedPercent === null ? null : weekly
}

export function providerAllowanceSummary(provider, allowance, now = new Date()) {
  if (provider === 'mobius' && typeof allowance?.usedPercent === 'number') {
    return [
      `${Math.round(allowance.usedPercent)}% used`,
      formatTrialTimeLeft(allowance.expiresAt, now),
    ].filter(Boolean).join(' · ')
  }
  if (typeof allowance?.usedPercent === 'number') {
    return [
      `${Math.round(allowance.usedPercent)}% ${allowance.label.toLowerCase()}`,
      allowance.stale ? 'last available' : '',
    ].filter(Boolean).join(' · ')
  }
  return allowance?.label || 'Usage'
}

/** Amber from 75% and red from 90%, so a limit that is close earns attention. */
export function usageTone(percent) {
  const value = clampUsagePercent(percent)
  if (value >= 90) return 'danger'
  if (value >= 75) return 'warning'
  return ''
}

// The overview row shows the allowance people plan around: the week when the
// plan has one, otherwise the 5-hour window, otherwise prepaid credit.
const ROW_USAGE = [
  { kind: 'weekly', label: 'Week', noun: 'weekly usage' },
  { kind: 'five_hour', label: '5 h', noun: '5-hour usage' },
  { kind: 'api_credits', label: 'Credit', noun: 'of credit used' },
]

/** The single usage figure for a provider's top-level Settings row, or null. */
export function rowUsage(snapshot, now = new Date()) {
  if (snapshot?.state !== 'ready' || !Array.isArray(snapshot.windows)) return null
  for (const { kind, label, noun } of ROW_USAGE) {
    const window = snapshot.windows.find(candidate => candidate?.kind === kind)
    const percent = currentUsedPercent(window, now)
    if (percent === null) continue
    const reset = formatUsageReset(window.resets_at)
    return {
      kind,
      label,
      percent,
      summary: [
        `${formatUsagePercent(percent)}% ${noun}`,
        reset,
        snapshot.stale === true ? 'last available reading' : '',
      ].filter(Boolean).join(' · '),
    }
  }
  return null
}

/**
 * "54036.32 credits" → "54,036 credits", "0.42 credits" → "0.4 credits": whole
 * credits from 10 up, one decimal below, and a positive balance never reads as
 * 0 ("<0.1 credits"). Anything unexpected is shown as given.
 */
export function formatCreditBalance(value) {
  if (typeof value !== 'string' || !value.trim()) return ''
  const match = value.trim().match(/^(-?\d+(?:\.\d+)?)\s*(.*)$/)
  if (!match) return value.trim()
  const amount = Number(match[1])
  if (!Number.isFinite(amount)) return value.trim()
  const format = new Intl.NumberFormat(undefined, { maximumFractionDigits: Math.abs(amount) < 10 ? 1 : 0 })
  const formatted = amount > 0 && amount < 0.05 ? `<${format.format(0.1)}` : format.format(amount)
  return match[2] ? `${formatted} ${match[2]}` : formatted
}

const DAY_MS = 86_400_000
const SIGN_IN_NOTICE_DAYS = 7
// The reported end of a Claude sign-in is an upper bound: the CLI treats it as
// a window, and the sign-in can end up to three days before it.
const SIGN_IN_EARLY_END_MS = 3 * DAY_MS

/**
 * A provider sign-in near its end (`login_expires_at`, ISO 8601) as a short
 * call to reconnect: `{ label, title, endsBy }` (`title` explains the date),
 * or null while it is further out or unknown, so a healthy sign-in stays
 * quiet. The countdown runs to the earliest possible end, three days before
 * the reported one.
 */
export function signInExpiryNotice(value, now = new Date()) {
  if (typeof value !== 'string' || !value) return null
  const endsBy = new Date(value)
  if (Number.isNaN(endsBy.getTime())) return null
  // Past the reported end the current access token may keep working for a
  // while, but renewing it can now fail: ask for a reconnect rather than
  // calling a connection that still runs chats expired.
  if (endsBy <= now) {
    return { label: 'Reconnect now', title: `Reported to end ${formatDateTime(endsBy)}; it may stop at any time`, endsBy }
  }
  const days = Math.ceil((endsBy - SIGN_IN_EARLY_END_MS - now) / DAY_MS)
  if (days > SIGN_IN_NOTICE_DAYS) return null
  const label = days <= 0
    ? 'Reconnect today'
    : days === 1 ? 'Reconnect within a day' : `Reconnect within ${days} days`
  return { label, title: `May end by ${formatDateTime(endsBy)}`, endsBy }
}
