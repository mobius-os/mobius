/* Project disjoint source spans through ONE full Markdown parse. Never lex a suffix. */
import { Marked } from 'marked'
import { mathTokens } from './mathTokens.js'

const md = new Marked()
md.use(mathTokens())
const segmenter = typeof Intl !== 'undefined' && Intl.Segmenter
  ? new Intl.Segmenter(undefined, { granularity: 'grapheme' }) : null
const reference = /^&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});/
const containers = new Set(['strong', 'em', 'del'])
const unsafeInline = tokens => tokens.some(token => token.type === 'html'
  || (Array.isArray(token.tokens) && unsafeInline(token.tokens)))

function boundary(source, offset) {
  if (!segmenter || !Number.isInteger(offset) || offset < 0 || offset > source.length) return false
  if (offset !== 0 && offset !== source.length) {
    let found = false
    for (const part of segmenter.segment(source)) {
      if (part.index === offset) { found = true; break }
      if (part.index > offset) break
    }
    if (!found) return false
  }
  const amp = source.slice(0, offset).lastIndexOf('&')
  if (amp >= 0) {
    const entity = reference.exec(source.slice(amp))
    if (entity && offset < amp + entity[0].length) return false
  }
  return true
}

function inlineProjection(tokens, source, base, start, end) {
  const result = []
  let cursor = base
  for (const token of tokens) {
    const raw = String(token?.raw ?? '')
    const next = cursor + raw.length
    if (!raw || source.slice(cursor, next) !== raw) return null
    if (start < next && end > cursor) {
      const lo = Math.max(start, cursor)
      const hi = Math.min(end, next)
      if (lo === cursor && hi === next) {
        result.push(token)
      } else if (token.type === 'text' && !Array.isArray(token.tokens)
        && token.text === raw && token.escaped !== true) {
        const piece = source.slice(lo, hi)
        result.push({ ...token, raw: piece, text: piece })
      } else if (containers.has(token.type) && Array.isArray(token.tokens)) {
        const innerRaw = token.tokens.map(child => String(child?.raw ?? '')).join('')
        const opening = raw.indexOf(innerRaw)
        if (!innerRaw || opening < 1 || raw.lastIndexOf(innerRaw) !== opening) return null
        const innerStart = cursor + opening
        const innerEnd = innerStart + innerRaw.length
        if (source.slice(innerStart, innerEnd) !== innerRaw
          || lo >= innerEnd || hi <= innerStart
          || (lo > cursor && lo <= innerStart)
          || (hi < next && hi > innerEnd)) return null
        const children = inlineProjection(token.tokens, source, innerStart,
          Math.max(lo, innerStart), Math.min(hi, innerEnd))
        if (!children?.length) return null
        result.push({ ...token, raw: source.slice(lo, hi),
          text: source.slice(Math.max(lo, innerStart), Math.min(hi, innerEnd)),
          tokens: children, rangeMarkup: {
            opening: raw.slice(0, opening), closing: raw.slice(opening + innerRaw.length),
          } })
      } else {
        return null
      }
    }
    cursor = next
  }
  if (start < cursor && end > cursor) return null
  return result
}

function project(tokens, source, start, end) {
  const out = []
  let cursor = 0
  for (const token of tokens) {
    const raw = String(token?.raw ?? '')
    const next = cursor + raw.length
    if (!raw || source.slice(cursor, next) !== raw) return null
    if (start < next && end > cursor) {
      const lo = Math.max(start, cursor)
      const hi = Math.min(end, next)
      if (lo === cursor && hi === next) {
        out.push(token)
      } else if ((token.type === 'paragraph' || token.type === 'heading')
        && Array.isArray(token.tokens)) {
        // Headings have source markers; paragraphs may have terminal newlines.
        // Only the exact inline source is positionable.
        const content = String(token.text ?? '')
        const offset = raw.indexOf(content)
        if (!content || offset < 0 || raw.lastIndexOf(content) !== offset) return null
        const contentStart = cursor + offset
        const contentEnd = contentStart + content.length
        if (unsafeInline(token.tokens)
          || (lo > cursor && lo <= contentStart)
          || (hi < next && hi >= contentEnd)) return null
        const children = inlineProjection(token.tokens, source, contentStart,
          Math.max(lo, contentStart), Math.min(hi, contentEnd))
        if (!children?.length) return null
        out.push({ ...token, raw: source.slice(lo, hi), text: source.slice(lo, hi),
          tokens: children, rangeStart: lo, rangeEnd: hi, rangeContext: raw,
          rangeMarkup: token.type === 'heading'
            ? { opening: '#'.repeat(token.depth) + ' ', closing: '\n' }
            : { opening: raw.slice(0, offset),
              closing: hi === next ? raw.slice(offset + content.length) : '' } })
      } else {
        return null
      }
    }
    cursor = next
  }
  return cursor === source.length ? out : null
}

/** Return null rather than guess if source positions or token shapes are unsafe. */
export function splitSteerMarkdown(text, cut) {
  if (typeof text !== 'string' || !boundary(text, cut) || cut <= 0 || cut > text.length) return null
  const tokens = md.lexer(text)
  const beforeTokens = project(tokens, text, 0, cut)
  const afterTokens = project(tokens, text, cut, text.length)
  if (!beforeTokens || !afterTokens) return null
  return {
    before: { source: text, start: 0, end: cut, tokens: beforeTokens },
    after: { source: text, start: cut, end: text.length, tokens: afterTokens },
  }
}

/** Tokens are derived from the full replay, never a standalone suffix parse. */
export function markdownRangeTokens(range) {
  return range?.tokens ?? []
}

/** Whole-fragment copy keeps source atoms (especially private image hrefs),
 * while balancing only the formatting that the range clipped. DOM media URLs
 * may be authorized URLs; they must never become clipboard source. */
export function markdownRangeSource(range) {
  function source(token) {
    // Literal text can gain Markdown meaning at a new fragment boundary (for
    // example "* tail" becomes a list). Escape text, not complete source atoms.
    if (token.type === 'text' && !token.tokens) return token.raw
      .replace(/\\/g, '\\\\').replace(/([`*_[\]~<>#+\-!|])/g, '\\$1')
      .replace(/(\d+)([.)])(?=\s)/g, '$1\\$2')
    if (!token.rangeMarkup) return token.raw
    const content = token.tokens.map(source).join('')
    const { opening, closing } = token.rangeMarkup
    if (!containers.has(token.type)) return opening + content + closing
    // Markdown emphasis cannot open/close beside whitespace. Keep that space
    // outside the wrapper without changing any selected characters.
    const [, leading, body, trailing] = /^(\s*)([\s\S]*?)(\s*)$/.exec(content)
    // Clipping nested *emphasis* may bring its stars together as **bold**.
    // Alternate the outer emphasis marker when the child touches that edge.
    const marker = token.type === 'em' && (body.startsWith(opening) || body.endsWith(closing))
      ? (opening === '*' ? '_' : '*') : null
    return leading + (body ? (marker || opening) + body + (marker || closing) : '') + trailing
  }
  return markdownRangeTokens(range).map(source).join('')
}

/** Slice using offsets relative to this descriptor's currently displayed raw span. */
export function sliceMarkdownRange(range, start, end) {
  if (!range || typeof range.source !== 'string'
    || !Number.isInteger(start) || !Number.isInteger(end)
    || start < 0 || end < start || end > range.end - range.start) return null
  if (start === 0 && end === range.end - range.start) return range
  const absoluteStart = range.start + start
  const absoluteEnd = range.start + end
  if (!boundary(range.source, absoluteStart) || !boundary(range.source, absoluteEnd)) return null
  if (absoluteStart === absoluteEnd) return { ...range, start: absoluteStart, end: absoluteEnd, tokens: [] }
  const tokens = project(md.lexer(range.source), range.source, absoluteStart, absoluteEnd)
  return tokens ? { source: range.source, start: absoluteStart, end: absoluteEnd, tokens } : null
}
