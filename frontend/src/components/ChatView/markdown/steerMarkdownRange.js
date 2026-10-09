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

function sourceDefinitions(tokens) {
  const definitions = new Map()
  md.walkTokens(tokens, token => {
    if (token.type === 'def') definitions.set(token.tag, token.raw)
  })
  return definitions
}

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
          tokens: children })
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
  if (cursor !== source.length) return null
  // Whole and clipped blocks share document-wide reference resolution. Raw
  // block text can stay identical while a later definition changes its links.
  // Keep this semantic context stable when only unrelated prose is appended.
  const rangeReferences = JSON.stringify(tokens.links)
  return out.map(token => ({ ...token, rangeReferences }))
}

/** Return null rather than guess if source positions or token shapes are unsafe. */
export function splitSteerMarkdown(text, cut) {
  if (typeof text !== 'string' || !boundary(text, cut) || cut <= 0 || cut > text.length) return null
  const tokens = md.lexer(text)
  const beforeTokens = project(tokens, text, 0, cut)
  const afterTokens = project(tokens, text, cut, text.length)
  if (!beforeTokens || !afterTokens) return null
  const definitions = sourceDefinitions(tokens)
  return {
    before: { source: text, start: 0, end: cut, tokens: beforeTokens, definitions },
    after: { source: text, start: cut, end: text.length, tokens: afterTokens, definitions },
  }
}

/** Tokens are derived from the full replay, never a standalone suffix parse. */
export function markdownRangeTokens(range) {
  return range?.tokens ?? []
}

/** Whole-fragment copy keeps source atoms (especially private image hrefs),
 * with standalone formatting at clipped boundaries. DOM media URLs
 * may be authorized URLs; they must never become clipboard source. */
export function markdownRangeSource(range) {
  const neededDefinitions = new Set()
  // Reference grammar comes from the same lexer as the source parse. Labels
  // use Marked's whitespace normalization and Unicode caseless matching.
  const rules = md.Lexer.rules.inline.gfm
  md.walkTokens(markdownRangeTokens(range), token => {
    if (token.type !== 'link' && token.type !== 'image') return
    const match = rules.reflink.exec(token.raw) || rules.nolink.exec(token.raw)
    if (!match || match[0] !== token.raw) return
    const tag = (match[2] || match[1]).replace(/\s+/g, ' ')
      .trim().toLowerCase().toUpperCase().toLowerCase()
    if (range.definitions?.has(tag)) neededDefinitions.add(tag)
  })
  function source(token, formats = []) {
    if (token.type === 'def') return ''
    // Literal text can gain Markdown meaning at a new fragment boundary (for
    // example "* tail" becomes a list). Escape text, not complete source atoms.
    if (token.type === 'text' && !token.tokens) return token.raw
      .replace(/\\/g, '\\\\').replace(/([`*_[\]~<>#+\-!|])/g, '\\$1')
      .replace(/(\d+)([.)])(?=\s)/g, '$1\\$2')
    if (!containers.has(token.type)) {
      if (!token.rangeMarkup) return token.raw
      const { opening, closing } = token.rangeMarkup
      return opening + token.tokens.map(child => source(child, formats)).join('') + closing
    }
    // Identical nested styles have the same visible effect. Normalize them in
    // clipboard Markdown: joined markers can change emphasis or start a fence.
    const inherited = formats.includes(token.type)
    const childFormats = inherited ? formats : [...formats, token.type]
    const content = token.tokens.map(child => source(child, childFormats)).join('')
    if (inherited) return content
    // Markdown emphasis cannot open/close beside whitespace. Keep that space
    // outside the wrapper without changing any selected characters.
    const [, leading, body, trailing] = /^(\s*)([\s\S]*?)(\s*)$/.exec(content)
    const marker = token.type === 'strong' ? '**' : token.type === 'em' ? '*' : '~~'
    return leading + (body ? marker + body + marker : '') + trailing
  }
  const content = markdownRangeTokens(range).map(token => source(token)).join('')
  // Only selected atoms bring their source definitions. Never append the
  // entire document's hidden targets or replace them with authorized DOM URLs.
  return neededDefinitions.size ? content.trimEnd() + '\n\n'
    + [...neededDefinitions].map(tag => range.definitions.get(tag)).join('\n') : content
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
  return tokens ? { source: range.source, start: absoluteStart, end: absoluteEnd,
    tokens, definitions: range.definitions } : null
}
