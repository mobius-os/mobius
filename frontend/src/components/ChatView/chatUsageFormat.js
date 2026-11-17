/**
 * Shared formatting for the Brain's chat-usage summary and detailed inspector.
 * One place keeps the progressive-disclosure layers numerically consistent.
 */

// Pick the unit and precision from the ROUNDED value, so 999,600 reads "1.0M"
// rather than "1000k", and 9,960 reads "10k" like every other value >= 10k.
export function formatTokenCount(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  if (Math.round(value) < 1000) return String(Math.round(value))
  const thousands = value / 1000
  if (thousands < 999.5) {
    return `${thousands.toFixed(thousands < 9.95 ? 1 : 0)}k`
  }
  const millions = value / 1_000_000
  return `${millions.toFixed(millions < 9.95 ? 1 : 0)}M`
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

export function formatUsageMenuText(totals, coverage) {
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
  const incomplete = incompleteRuns(coverage) > 0
  if (!parts.length) return incomplete ? 'Usage unknown' : null
  return `${incomplete ? 'At least ' : ''}${parts.join(' · ')}`
}

export function modelDisplayName(modelId, registry, provider) {
  if (typeof modelId !== 'string' || !modelId.trim()) return null
  const id = modelId.trim()
  const preferred = Array.isArray(registry?.[provider]) ? registry[provider] : []
  const providerMatch = preferred.find(candidate => candidate?.id === id)
  if (typeof providerMatch?.label === 'string' && providerMatch.label.trim()) {
    return providerMatch.label.trim()
  }
  for (const entries of Object.values(registry || {})) {
    if (!Array.isArray(entries) || entries === preferred) continue
    const match = entries.find(candidate => candidate?.id === id)
    if (typeof match?.label === 'string' && match.label.trim()) {
      return match.label.trim()
    }
  }
  return id
}

export function usageModelName(usage, registry = null, provider = null) {
  if (typeof usage?.model === 'string' && usage.model.trim()) {
    return modelDisplayName(usage.model, registry, provider)
  }
  const models = Object.keys(usage?.provider_model_usage || {})
  if (!models.length) return null
  return models
    .map(model => modelDisplayName(model, registry, provider))
    .filter(Boolean)
    .join(', ')
}

export function formatTimestamp(value) {
  if (!value) return null
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return null
  return formatDateTime(date)
}
import { formatDateTime } from '../../lib/dateTimeFormat.js'

// Per-turn usage coverage (backend usage_metrics.USAGE_QUALITIES). A complete
// aggregate needs no label; every other kind says what its numbers mean.
const USAGE_QUALITY_LABELS = {
  partial: 'Partial (at least this much)',
  unknown: 'Not known for this turn',
}

export function usageQualityLabel(quality) {
  return USAGE_QUALITY_LABELS[quality] || null
}

// Turns whose usage is a lower bound or not counted. In-flight and untracked
// turns (missing) have their own note and never mark the totals incomplete.
function incompleteRuns(coverage) {
  const byQuality = coverage?.runs_by_quality || {}
  return Object.keys(USAGE_QUALITY_LABELS)
    .reduce((sum, quality) => sum + (Number(byQuality[quality]) || 0), 0)
}

/** Why chat totals are a lower bound rather than a complete aggregate. */
export function incompleteUsageNote(coverage) {
  const count = incompleteRuns(coverage)
  if (count <= 0) return null
  const turns = count === 1 ? '1 turn has' : `${count} turns have`
  return `${turns} partial or unknown usage, so totals are a lower bound.`
}
