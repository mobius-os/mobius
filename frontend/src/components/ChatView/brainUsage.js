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

export function formatRoundedTokenCount(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return ''
  if (Math.abs(value) < 500) return '0'
  const thousands = Math.round(value / 1_000)
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 0 }).format(thousands)}k`
}

export function contextUsedPercent(snapshot) {
  const tokens = contextTokenCounts(snapshot)
  return tokens === null ? null : clampPercent((tokens.used / tokens.maximum) * 100)
}
