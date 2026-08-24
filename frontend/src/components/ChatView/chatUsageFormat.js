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

export function formatCostUsd(value, { compact = false } = {}) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null
  if (value <= 0) return compact ? '$0' : '$0.00'
  if (value < 0.01) return '<$0.01'
  if (compact && value >= 10) return `$${Math.round(value)}`
  return `$${value.toFixed(2)}`
}

// The composer badge: cost is the number owners actually think in, tokens
// are the secondary detail available on hover/aria-label and in the full
// inspector.
export function formatUsageBadge(totals) {
  if (!totals) return null
  const cost = formatCostUsd(totals.cost_usd, { compact: true })
  if (cost !== null) return cost
  const tokens = formatTokenCount(totals.total_tokens)
  return tokens ? `${tokens} tok` : null
}

// The subtle always-visible strip above the composer: cost plus the input/
// output split, since "other details" (not just the total) was explicitly
// asked for. Exact (non-compact) cost — this line has room, unlike the
// icon badge it replaced.
export function formatUsageStripText(totals) {
  if (!totals) return null
  const cost = formatCostUsd(totals.cost_usd)
  const total = formatTokenCount(totals.total_tokens)
  const input = formatTokenCount(totals.input_tokens)
  const output = formatTokenCount(totals.output_tokens)
  if (cost === null && !total) return null
  const parts = []
  if (cost !== null) parts.push(cost)
  if (total) {
    parts.push(input && output
      ? `${total} tokens (${input} in · ${output} out)`
      : `${total} tokens`)
  }
  return parts.join(' · ')
}

export function formatUsageAriaSummary(totals) {
  if (!totals) return 'Usage not yet available for this chat'
  const cost = formatCostUsd(totals.cost_usd)
  const tokens = formatTokenCount(totals.total_tokens)
  const parts = []
  if (cost !== null) parts.push(`cost ${cost}`)
  if (tokens) parts.push(`${tokens} tokens`)
  return parts.length ? `Chat usage so far: ${parts.join(', ')}` : 'Usage not yet available for this chat'
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
