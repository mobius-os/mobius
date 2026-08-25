/**
 * Shared formatting for the chat token-usage/cost surfaces (the composer
 * badge and the full ChatUsageInspector breakdown). One place so the badge's
 * compact numbers and the inspector's precise ones never drift apart.
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

export function formatUsageStripText(totals) {
  if (!totals) return null
  const cost = formatCostUsd(totals.cost_usd)
  const input = formatTokenCount(totals.input_tokens)
  const output = formatTokenCount(totals.output_tokens)
  const total = formatTokenCount(totals.total_tokens)
  if (cost === null && !input && !output && !total) return null
  const parts = ['Usage']
  if (cost !== null) parts.push(cost)
  if (input || output) {
    parts.push([
      input && `${input} in`,
      output && `${output} out`,
    ].filter(Boolean).join(' / '))
  } else if (total) parts.push(`${total} tokens`)
  return parts.join(' · ')
}

export function formatUsageAriaSummary(totals) {
  if (!totals) return 'Usage not yet available for this chat'
  const cost = formatCostUsd(totals.cost_usd)
  const tokens = formatTokenCount(totals.total_tokens)
  const parts = []
  if (cost !== null) parts.push(`reported cost ${cost}`)
  if (tokens) parts.push(`${tokens} tokens`)
  return parts.length ? `Chat usage so far: ${parts.join(', ')}` : 'Usage not yet available for this chat'
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
