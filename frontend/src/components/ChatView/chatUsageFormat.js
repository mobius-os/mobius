/**
 * Shared formatting for the Brain's chat-usage summary and detailed inspector.
 * One place keeps the progressive-disclosure layers numerically consistent.
 */

export function formatTokenCount(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  if (value < 1000) return String(Math.round(value))
  if (value < 1_000_000) {
    const thousands = value / 1000
    return `${thousands.toFixed(thousands < 10 ? 1 : 0)}k`
  }
  const millions = value / 1_000_000
  return `${millions.toFixed(millions < 10 ? 1 : 0)}M`
}

export function formatCostUsd(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  if (value <= 0) return '$0.00'
  if (value < 0.01) return '<$0.01'
  return `$${value.toFixed(2)}`
}

export function nonCachedInputTokens(totals) {
  if (typeof totals?.input_tokens !== 'number'
      || !Number.isFinite(totals.input_tokens)) return null
  const cached = typeof totals.cache_read_input_tokens === 'number'
    && Number.isFinite(totals.cache_read_input_tokens)
    ? totals.cache_read_input_tokens
    : 0
  return Math.max(0, totals.input_tokens - cached)
}

export function cacheHitRate(totals) {
  const input = totals?.input_tokens
  const cached = totals?.cache_read_input_tokens
  if (typeof input !== 'number' || !Number.isFinite(input) || input <= 0) return null
  if (typeof cached !== 'number' || !Number.isFinite(cached)) return null
  return Math.min(100, Math.max(0, (cached / input) * 100))
}

export function formatCacheHitRate(totals, fractionDigits = 0) {
  const rate = cacheHitRate(totals)
  if (rate === null) return null
  const digits = Math.max(0, Math.min(1, fractionDigits))
  return `${rate.toFixed(digits).replace(/\.0$/, '')}%`
}

export function formatUsageMenuText(totals) {
  if (!totals) return null
  const input = formatTokenCount(nonCachedInputTokens(totals))
  const output = formatTokenCount(totals.output_tokens)
  const cache = formatCacheHitRate(totals)
  const cost = formatCostUsd(totals.cost_usd)
  const parts = []
  if (input) parts.push(`${input} in`)
  if (output) parts.push(`${output} out`)
  if (cache) parts.push(`${cache} cache`)
  if (cost !== null) parts.push(cost)
  if (!parts.length) return null
  return parts.join(' · ')
}

export function usageModelName(usage) {
  if (typeof usage?.model === 'string' && usage.model.trim()) {
    return usage.model.trim()
  }
  const models = Object.keys(usage?.provider_model_usage || {})
  return models.length ? models.join(', ') : null
}

export function formatTimestamp(value) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return date.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}
