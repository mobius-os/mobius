/** Pure conversions for the composer's two brain gauges. */

function clampPercent(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  return Math.min(100, Math.max(0, value))
}

export function visibleBrainFillBounds(percent, { top, bottom, inset }) {
  const visibleTop = top + inset
  const visibleBottom = bottom - inset
  const clamped = clampPercent(percent) ?? 0
  const fillHeight = ((visibleBottom - visibleTop) * clamped) / 100
  return {
    fillHeight,
    fillY: visibleBottom - fillHeight,
  }
}

export function contextTokenCounts(snapshot) {
  const input = snapshot?.input_tokens
  const window = snapshot?.context_window
  if (
    typeof input !== 'number'
    || !Number.isFinite(input)
    || typeof window !== 'number'
    || !Number.isFinite(window)
    || window <= 0
  ) {
    return null
  }
  return {
    used: Math.max(0, Math.round(input)),
    maximum: Math.round(window),
  }
}

export function modelContextTokenCounts(registry, provider, model) {
  const models = registry?.[provider]
  if (!Array.isArray(models) || typeof model !== 'string') return null
  const entry = models.find(candidate => candidate?.id === model)
  const maximum = entry?.context_window
  if (typeof maximum !== 'number' || !Number.isFinite(maximum) || maximum <= 0) {
    return null
  }
  return { used: 0, maximum: Math.round(maximum) }
}

export function resolvedContextTokenCounts(snapshot, registry, provider, model) {
  if (!snapshot || snapshot.provider !== provider) return null
  const live = contextTokenCounts(snapshot)
  if (live !== null) return live
  // The registry can prove the ceiling, but only the server can prove that a
  // chat has used no context yet. An established session with missing usage is
  // unknown—not an empty context—and a failed request has no snapshot at all.
  return snapshot.provider_session_id === null
    ? modelContextTokenCounts(registry, provider, model)
    : null
}

// Descending unit steps so a count keeps at most three digits before its
// symbol: thousands (k), millions (M), billions (G).
const TOKEN_UNITS = [
  { limit: 1_000_000_000, symbol: 'G' },
  { limit: 1_000_000, symbol: 'M' },
  { limit: 1_000, symbol: 'k' },
]

// One decimal below ten (1.4M) keeps three significant digits, whole numbers
// above; drop a bare ".0" so a round magnitude reads as "1M" not "1.0M".
function _scaledText(value, limit) {
  const scaled = value / limit
  return scaled.toFixed(Math.abs(scaled) < 10 ? 1 : 0)
}

export function formatRoundedTokenCount(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return ''
  if (Math.abs(value) < 500) return '0'
  for (let i = 0; i < TOKEN_UNITS.length; i++) {
    if (Math.abs(value) < TOKEN_UNITS[i].limit) continue
    let { symbol } = TOKEN_UNITS[i]
    let text = _scaledText(value, TOKEN_UNITS[i].limit)
    // Rounding a value just under the next threshold (999_999) can carry the
    // scaled count to four digits ("1000k"); that magnitude reads as one unit
    // up ("1M"). The carry is at most a single step, so promote once.
    if (i > 0 && Math.abs(Number(text)) >= 1_000) {
      symbol = TOKEN_UNITS[i - 1].symbol
      text = _scaledText(value, TOKEN_UNITS[i - 1].limit)
    }
    return `${text.replace(/\.0$/, '')}${symbol}`
  }
  // 500–999 rounds up into the smallest unit instead of showing bare digits.
  return `${Math.round(value / 1_000)}k`
}

export function contextUsedPercent(snapshot) {
  const tokens = contextTokenCounts(snapshot)
  return tokens === null ? null : clampPercent((tokens.used / tokens.maximum) * 100)
}
