/* Decide whether an exact steer-replay cut is safe for two Markdown segments. */

import { Marked } from 'marked'
import { mathTokens } from './mathTokens.js'

const md = new Marked()
md.use(mathTokens())
const graphemeSegmenter = (
  typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function'
)
  ? new Intl.Segmenter(undefined, { granularity: 'grapheme' })
  : null
const MARKDOWN_CONTROL = /[\\`*_~\[\]<>$&|]/
const URLISH_TAIL = /(?:https?:\/\/|www\.)\S*$/i
const EMAILISH_TAIL = /[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]*$/


function isGraphemeBoundary(source, cut) {
  // Modern Möbius runtimes expose Intl.Segmenter. An older engine fails closed
  // on nonterminal splits rather than guessing at a Unicode boundary.
  if (!graphemeSegmenter) return false
  for (const segment of graphemeSegmenter.segment(source)) {
    if (segment.index === cut) return true
    if (segment.index > cut) return false
  }
  return cut === source.length
}


function splitsCharacterReference(source, cut) {
  const amp = source.slice(0, cut).lastIndexOf('&')
  if (amp < 0) return false
  const match = /^&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});/
    .exec(source.slice(amp))
  return !!(match && cut < amp + match[0].length)
}


function isFutureStablePlainPrefix(rawPrefix) {
  // Marked treats an unmatched delimiter as ordinary text until later input
  // closes it. Trimming that temporary shape would be non-monotonic: the
  // duplicate prefix would disappear, then reappear when the parser learned
  // it was emphasis/link/code/math. Keep those literal controls intact.
  const withoutCompleteReferences = rawPrefix.replace(
    /&(?:#[0-9]{1,7}|#[xX][0-9a-fA-F]{1,6}|[A-Za-z][A-Za-z0-9]{1,31});/g,
    '',
  )
  return !MARKDOWN_CONTROL.test(withoutCompleteReferences)
    && !URLISH_TAIL.test(withoutCompleteReferences)
    && !EMAILISH_TAIL.test(withoutCompleteReferences)
}


function isFutureStableParagraphEnd(block) {
  const tokens = Array.isArray(block?.tokens) ? block.tokens : []
  const last = tokens[tokens.length - 1]
  if (!last) return true
  const raw = String(last.raw || '')
  if (last.type === 'text' && !Array.isArray(last.tokens)) {
    return isFutureStablePlainPrefix(raw)
  }
  // Marked recognizes a bare URL/email as a link before the provider has
  // necessarily finished typing it. Delimited links are closed constructs;
  // bare ones may still grow and would move the safe boundary.
  if (last.type === 'link') {
    return !/^(?:https?:\/\/|www\.)/i.test(raw) && !EMAILISH_TAIL.test(raw)
  }
  return true
}


/**
 * A steer can split a plain word at any character, but it must not split an
 * open Markdown construct. Both transcript segments are rendered separately;
 * cutting inside emphasis, a link, code, math, a list, or another container
 * would make the suffix lose the syntax opened above the user row.
 *
 * `cut` is a raw-source offset in `text`. Fail closed on unknown token shapes.
 */
export function safeSteerMarkdownCut(text, cut) {
  if (!Number.isInteger(cut) || cut <= 0 || cut > String(text || '').length) {
    return false
  }
  const source = String(text || '')
  if (!isGraphemeBoundary(source, cut) || splitsCharacterReference(source, cut)) {
    return false
  }

  const blocks = md.lexer(source)
  let blockStart = 0
  for (const block of blocks) {
    const raw = String(block?.raw || '')
    const blockEnd = blockStart + raw.length
    if (cut === blockStart || (cut === blockEnd && blockEnd < source.length)) {
      return true
    }
    if (cut < blockStart || cut > blockEnd) {
      blockStart = blockEnd
      continue
    }

    // A top-level paragraph's direct plain-text token is the one construct
    // that can be split mid-token without carrying syntax across the user row.
    // Container blocks deliberately fail closed even when their leaf happens
    // to be text: the list/quote/heading/code syntax still opened above it.
    if (block?.type !== 'paragraph' || !Array.isArray(block.tokens)) {
      return cut === blockEnd
    }
    if (cut === blockEnd && blockEnd === source.length) {
      return isFutureStableParagraphEnd(block)
    }
    const localCut = cut - blockStart
    let inlineStart = 0
    for (const token of block.tokens) {
      const inlineRaw = String(token?.raw || '')
      const inlineEnd = inlineStart + inlineRaw.length
      if (localCut === inlineStart) return true
      if (localCut > inlineStart && localCut <= inlineEnd) {
        if (token?.type !== 'text' || Array.isArray(token.tokens)) {
          return localCut === inlineEnd
        }
        return isFutureStablePlainPrefix(
          inlineRaw.slice(0, localCut - inlineStart),
        )
      }
      inlineStart = inlineEnd
    }
    // Marked may leave only trailing paragraph whitespace outside inline
    // tokens. A cut there carries no Markdown state.
    return localCut >= inlineStart && localCut <= raw.length
  }
  return false
}
