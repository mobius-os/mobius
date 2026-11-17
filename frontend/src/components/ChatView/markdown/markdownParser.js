/* The chat's one Markdown parser: every surface that lexes chat Markdown (the
   renderer, steer-replay cuts, Copy's plain text for text not on screen) uses
   these instances, so they cannot read the same text differently.

   Configured marked instances with math token support. Tokenization stays tiny
   and synchronous; the renderer loads KaTeX only when a math token is present.

   Owner messages and replies share this one formatter. Owner-written text
   differs in exactly two ways, both so the bubble shows what was typed:
     - a typed single line break stays a line break (marked's `breaks`), where
       replies keep CommonMark's soft-break joining;
     - tag-like text stays literal text. The renderer drops raw HTML tokens,
       which is right for replies but would silently hide pasted `Promise<void>`
       or a stack trace's `<anonymous>`. A tokenizer returning undefined (not
       false, which falls back to the default) leaves that text to the
       paragraph and text tokenizers; autolinks such as `<https://…>` still work.
   Agent text shown outside a reply, such as a question card, also keeps
   tag-like text literal (`literalTags`): a choice like `Return Promise<void>`
   must read the same as the answer it submits.
   These are separate instances rather than per-call options because
   `Marked#lexer(src, options)` replaces the instance defaults, extensions
   included. */
import { Marked } from 'marked'
import { mathTokens } from './mathTokens.js'

function configuredMarked(options = {}) {
  const instance = new Marked(options)
  instance.use(mathTokens())
  return instance
}

const literalTagTokenizer = { html: () => undefined, tag: () => undefined }
/** The reply parser: chat Markdown with math. */
export const md = configuredMarked()
const mdOwnerText = configuredMarked({ breaks: true, tokenizer: literalTagTokenizer })
const mdLiteralTags = configuredMarked({ tokenizer: literalTagTokenizer })
// Agent-written chat notes are plain Markdown: a `$` there is a shell variable
// or a price, never TeX, so they lex without math tokens.
const plainMd = new Marked()

export function tokenize(text, { writtenByOwner = false, literalTags = false, math = true } = {}) {
  if (!math) return plainMd.lexer(text || '')
  if (writtenByOwner) return mdOwnerText.lexer(text || '')
  return (literalTags ? mdLiteralTags : md).lexer(text || '')
}

/** One-line agent text such as a choice label: emphasis, code and math only.
 *  `1. Fast` or `# Plan` stay text, tag-like text stays literal, and a link or
 *  image is only its text. */
export function inlineTokens(text) {
  return textOnly(mdLiteralTags.Lexer.lexInline(text || '', mdLiteralTags.defaults))
}

function textOnly(tokens) {
  return tokens.flatMap(token => {
    if (token.type === 'link') return textOnly(token.tokens || [])
    if (token.type === 'image') return token.text ? [{ type: 'text', text: token.text }] : []
    return token.tokens ? [{ ...token, tokens: textOnly(token.tokens) }] : [token]
  })
}

export function markdownImageHrefs(text) {
  const hrefs = []
  md.walkTokens(tokenize(text), token => {
    if (token.type === 'image') hrefs.push(token.href)
  })
  return hrefs
}

const escapeHtml = text => text.replace(/[&<>"]/g, char => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[char]))
// Math renders as the TeX annotation KaTeX carries, which is what the
// clipboard rules read from the transcript.
const texHtml = (tag, className) => token => `<${tag} class="${className}">`
  + `<annotation encoding="application/x-tex">${escapeHtml(token.text)}</annotation></${tag}>`
// Renders the tokens above as plain HTML, without the live renderer's effects
// or app context, so an image keeps its alt text and an app link stays a link.
// Raw HTML renders as nothing, as in the transcript: an HTML block is not
// shown, and an inline tag drops while the text between tags stays.
const htmlRenderer = new Marked({ gfm: true }).use({ extensions: [
  { name: 'inlineKatex', renderer: texHtml('span', 'katex') },
  { name: 'blockKatex', renderer: texHtml('div', 'md-math-block') },
], renderer: { html: () => '' } })

/** Markdown as plain HTML, lexed exactly as `tokenize` lexes it. */
export function markdownHtml(text, options) {
  return htmlRenderer.parser(tokenize(text, options))
}

/** One line of agent text as plain HTML, lexed as `inlineTokens` lexes it. */
export function inlineMarkdownHtml(text) {
  return htmlRenderer.Parser.parseInline(inlineTokens(text), htmlRenderer.defaults)
}
