// Pure derivation behind the MessageSources component, kept separate so the
// collection/dedupe contract is directly testable.
//
// Live source data rides on the turn's tool blocks: both runners emit a
// `tool_sources` event and the reducer stamps it onto the WebSearch block.
// Historical compact reads replace those arrays with a message-level
// `source_ref`; MessageSources fetches the same persisted metadata only when
// its References disclosure opens. Deriving rather than adding a `sources`
// content block is deliberate:
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

// Sites commonly expose a default icon at their origin root. SourceFavicon owns
// the authenticated proxy read; this helper only derives that conventional
// target, keeping us independent of third-party favicon services.
export function sourceFaviconUrl(url) {
  try {
    const safeUrl = safeSourceUrl(url)
    return safeUrl ? new URL('/favicon.ico', safeUrl).href : ''
  } catch {
    return ''
  }
}

// Declared-icon discovery fetches only the site's origin, not the full cited
// article path. Besides preserving reader privacy, this gives repeated links
// from one host the same request/cache key.
export function sourceFaviconDiscoveryUrl(url) {
  try {
    const safeUrl = safeSourceUrl(url)
    return safeUrl ? new URL('/', safeUrl).href : ''
  } catch {
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

function sourceUrlParts(source) {
  try {
    const parsed = new URL(safeSourceUrl(source?.url))
    return {
      host: parsed.host.replace(/^www\./i, ''),
      parts: parsed.pathname
        .split('/')
        .filter(Boolean)
        .map(part => {
          try { return decodeURIComponent(part) } catch { return part }
        }),
    }
  } catch {
    return { host: '', parts: [], fallback: source?.url || '' }
  }
}

function boundedSourceHint(value) {
  if (value.length <= 96) return value
  return `${value.slice(0, 47)}…${value.slice(-48)}`
}

function sourceUrlHint(parsed, expanded = false) {
  const path = expanded
    ? parsed.parts.join('/')
    : parsed.parts.at(-1) || ''
  if (path && parsed.host) return boundedSourceHint(`${path} · ${parsed.host}`)
  return boundedSourceHint(path || parsed.host || parsed.fallback || '')
}

/** Keep every distinct citation while making repeated visible titles honest.
 * Search results commonly reuse generic titles across different documents;
 * URL-only dedupe correctly preserves those links, but identical chips make
 * them look like duplicated UI. Only colliding labels gain a compact URL hint,
 * placed first so the chip's ellipsis cannot hide the distinguishing part.
 */
export function sourceDisplayLabels(sources) {
  if (!Array.isArray(sources)) return []
  const baseLabels = sources.map(sourceLabel)
  const indexesByLabel = new Map()
  baseLabels.forEach((label, index) => {
    const indexes = indexesByLabel.get(label) || []
    indexes.push(index)
    indexesByLabel.set(label, indexes)
  })

  const labels = [...baseLabels]
  for (const [baseLabel, indexes] of indexesByLabel) {
    if (indexes.length === 1) continue
    const parsed = indexes.map(index => sourceUrlParts(sources[index]))
    const compactHints = parsed.map(parts => sourceUrlHint(parts))
    const compactCounts = new Map()
    for (const hint of compactHints) {
      compactCounts.set(hint, (compactCounts.get(hint) || 0) + 1)
    }
    const hints = compactHints.map((hint, index) => (
      compactCounts.get(hint) > 1 ? sourceUrlHint(parsed[index], true) : hint
    ))
    const hintCounts = new Map()
    for (const hint of hints) {
      hintCounts.set(hint, (hintCounts.get(hint) || 0) + 1)
    }
    const occurrences = new Map()
    hints.forEach((hint, groupIndex) => {
      let distinctHint = hint
      if (hintCounts.get(hint) > 1) {
        const occurrence = (occurrences.get(hint) || 0) + 1
        occurrences.set(hint, occurrence)
        distinctHint = `${hint} (${occurrence})`
      }
      labels[indexes[groupIndex]] = `${distinctHint} — ${baseLabel}`
    })
  }
  return labels
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
    // Activity support keeps older stored transcripts readable. New compact
    // reads omit these arrays and expose a source_ref for on-demand detail.
    if (!['tool', 'activity'].includes(block?.type)
        || !Array.isArray(block.sources)) continue
    for (const rawSource of block.sources) {
      scannedRows += 1
      if (scannedRows > MAX_SOURCE_ROWS_SCANNED) break outer
      const source = boundedMessageSource(rawSource)
      // The backend enforces http(s) (tool_sources.py `_safe_http_url`), but
      // this value ends up in an <a href> after disclosure expansion, so
      // re-check the scheme here instead of trusting the live and lazy
      // upstream call sites to stay correct forever.
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
