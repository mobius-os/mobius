// Pure derivation behind the MessageSources component, kept separate so the
// collection/dedupe contract is directly testable.
//
// The data already rides on the turn's tool blocks: both runners emit a
// `tool_sources` event, backend events.py stamps it onto the WebSearch tool
// block, and `_persisted_block` keeps tool blocks whole — so these survive
// streaming, promotion, and reload with no second pathway and no migration.
// Deriving rather than adding a `sources` content block is deliberate:
// `tool_sources` is in the reducer's mutate-only, thinking-transparent set
// (events.py `_THINKING_INTERRUPTING_TYPES`), so appending a sibling block
// from it would fragment one continuous reasoning pass into many one-second
// "Thought" blocks.

// Only complete http(s) URLs may reach an href. URL() rejects superficially
// plausible but unusable values such as `https://` and hosts with whitespace;
// checking the parsed protocol rejects javascript:/data:/mailto:.
export function safeSourceUrl(value) {
  if (typeof value !== 'string') return ''
  const candidate = value.trim()
  if (!candidate) return ''
  try {
    const parsed = new URL(candidate)
    return ['http:', 'https:'].includes(parsed.protocol) && parsed.host
      ? candidate
      : ''
  } catch {
    return ''
  }
}

export function sourceHost(url) {
  try {
    const safeUrl = safeSourceUrl(url)
    return safeUrl ? new URL(safeUrl).host : ''
  } catch {
    // Unparseable URLs have no meaningful host chip; the title keeps the label.
    return ''
  }
}

// What the chip actually reads. A title is only sometimes available: Claude's
// WebSearch result carries title + snippet, but Codex's WebSearchThreadItem
// exposes a URL only on its `openPage` / `findInPage` actions and never a
// title. Falling back to the raw URL would print the whole link as the label
// with its own host repeated beside it, so a title-less source reads as its
// host instead.
export function sourceLabel(source) {
  const title = typeof source?.title === 'string' ? source.title.trim() : ''
  if (title && title !== source?.url) return title
  return sourceHost(source?.url) || source?.url || ''
}

// First occurrence wins, so the order the agent actually searched in is kept.
// The backend normalizer already dedupes within one tool result; this dedupes
// ACROSS the several searches a turn usually makes.
export function messageSources(blocks) {
  if (!Array.isArray(blocks)) return []
  const seen = new Set()
  const sources = []
  for (const block of blocks) {
    if (block?.type !== 'tool' || !Array.isArray(block.sources)) continue
    for (const source of block.sources) {
      const url = safeSourceUrl(source?.url)
      // The backend enforces http(s) (tool_sources.py `_safe_http_url`), but
      // this value ends up in an <a href> that now renders unconditionally
      // rather than behind a disclosure, so re-check the scheme here instead
      // of trusting two upstream call sites to stay correct forever.
      if (!url) continue
      if (seen.has(url)) continue
      seen.add(url)
      sources.push(url === source.url ? source : { ...source, url })
    }
  }
  return sources
}
