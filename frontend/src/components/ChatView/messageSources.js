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

export const MAX_MESSAGE_SOURCES = 24
export const MAX_SOURCE_URL_CHARS = 2048
export const MAX_SOURCE_TITLE_CHARS = 300
export const MAX_SOURCE_SNIPPET_CHARS = 700
const MAX_SOURCE_ROWS_SCANNED = 512

// Only complete http(s) URLs may reach an href. URL() rejects superficially
// plausible but unusable values such as `https://` and hosts with whitespace;
// checking the parsed protocol rejects javascript:/data:/mailto:.
export function safeSourceUrl(value) {
  if (typeof value !== 'string') return ''
  // Avoid allocating a second huge string just to reject malformed metadata.
  if (value.length > MAX_SOURCE_URL_CHARS + 64) return ''
  const candidate = value.trim()
  if (!candidate || candidate.length > MAX_SOURCE_URL_CHARS) return ''
  try {
    const parsed = new URL(candidate)
    return ['http:', 'https:'].includes(parsed.protocol) && parsed.host
      ? candidate
      : ''
  } catch {
    return ''
  }
}

export function boundedMessageSource(source) {
  const url = safeSourceUrl(source?.url)
  if (!url) return null
  const title = typeof source?.title === 'string'
    ? source.title.slice(0, MAX_SOURCE_TITLE_CHARS).trim()
    : ''
  const snippet = typeof source?.snippet === 'string'
    ? source.snippet.slice(0, MAX_SOURCE_SNIPPET_CHARS).trim()
    : ''
  // The normal live path is already normalized by the backend. Preserve that
  // object identity so streaming text ticks do not allocate replacement source
  // objects; only legacy/malformed values pay for a bounded copy.
  if (url === source.url
      && (source.title == null || title === source.title)
      && (source.snippet == null || snippet === source.snippet)) {
    return source
  }
  return {
    ...(title ? { title } : {}),
    url,
    ...(snippet ? { snippet } : {}),
  }
}

export function enrichMessageSource(existing, incoming) {
  const currentTitle = existing.title || ''
  const incomingTitle = incoming.title || ''
  const betterTitle = (!currentTitle || currentTitle === existing.url)
    && incomingTitle && incomingTitle !== incoming.url
  const betterSnippet = !existing.snippet && incoming.snippet
  if (!betterTitle && !betterSnippet) return existing
  return {
    ...existing,
    ...(betterTitle ? { title: incomingTitle } : {}),
    ...(betterSnippet ? { snippet: incoming.snippet } : {}),
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

// First occurrence owns the position, so search order is kept. A later copy may
// fill missing title/snippet metadata without moving or duplicating the card.
export function messageSources(blocks) {
  if (!Array.isArray(blocks)) return []
  const indexByUrl = new Map()
  const sources = []
  let scannedRows = 0
  outer:
  for (const block of blocks) {
    // Compact historical activity carries the same bounded source metadata on
    // its summary block, so citations remain visible without loading the full
    // tool timeline merely to rediscover them.
    if (!['tool', 'activity'].includes(block?.type)
        || !Array.isArray(block.sources)) continue
    for (const rawSource of block.sources) {
      scannedRows += 1
      if (scannedRows > MAX_SOURCE_ROWS_SCANNED) break outer
      const source = boundedMessageSource(rawSource)
      // The backend enforces http(s) (tool_sources.py `_safe_http_url`), but
      // this value ends up in an <a href> that now renders unconditionally
      // rather than behind a disclosure, so re-check the scheme here instead
      // of trusting two upstream call sites to stay correct forever.
      if (!source) continue
      const existingIndex = indexByUrl.get(source.url)
      if (existingIndex != null) {
        sources[existingIndex] = enrichMessageSource(
          sources[existingIndex], source,
        )
        continue
      }
      if (sources.length >= MAX_MESSAGE_SOURCES) continue
      indexByUrl.set(source.url, sources.length)
      sources.push(source)
    }
  }
  return sources
}
