import { useMemo } from 'react'
import { Marked } from 'marked'
import { MemoBlock, MathBlock } from './blocks.jsx'
import InlineContent from './InlineContent.jsx'
import { mathTokens } from './mathTokens.js'
import { groupMarkdownImages } from './imageGallery.js'
import ImageGallery from './ImageGallery.jsx'
import AppLinkCard from './AppLinkCard.jsx'
import { appLinkCardFromParagraph } from './appLinkCard.js'
import { markdownRangeTokens } from './steerMarkdownRange.js'
import '../markdown.css'

/**
 * Configured marked instances with math token support. Tokenization stays tiny
 * and synchronous; the renderer loads KaTeX only when a math token is present.
 *
 * Owner messages and replies share this one formatter. Owner-written text
 * differs in exactly two ways, both so the bubble shows what was typed:
 *   - a typed single line break stays a line break (marked's `breaks`), where
 *     replies keep CommonMark's soft-break joining;
 *   - tag-like text stays literal text. The renderer drops raw HTML tokens,
 *     which is right for replies but would silently hide pasted `Promise<void>`
 *     or a stack trace's `<anonymous>`. A tokenizer returning undefined (not
 *     false, which falls back to the default) leaves that text to the
 *     paragraph and text tokenizers; autolinks such as `<https://…>` still work.
 * Agent text shown outside a reply, such as a question card, also keeps
 * tag-like text literal (`literalTags`): a choice like `Return Promise<void>`
 * must read the same as the answer it submits.
 * These are separate instances rather than per-call options because
 * `Marked#lexer(src, options)` replaces the instance defaults, extensions
 * included.
 */
function configuredMarked(options = {}) {
  const instance = new Marked(options)
  instance.use(mathTokens())
  return instance
}

const literalTagTokenizer = { html: () => undefined, tag: () => undefined }
const md = configuredMarked()
const mdOwnerText = configuredMarked({ breaks: true, tokenizer: literalTagTokenizer })
const mdLiteralTags = configuredMarked({ tokenizer: literalTagTokenizer })
// Agent-written chat notes are plain Markdown: a `$` there is a shell variable
// or a price, never TeX, so they lex without math tokens.
const plainMd = new Marked()


export function markdownImageHrefs(text) {
  const hrefs = []
  md.walkTokens(tokenize(text), token => {
    if (token.type === 'image') hrefs.push(token.href)
  })
  return hrefs
}

function tokenize(text, { writtenByOwner = false, literalTags = false, math = true } = {}) {
  if (!math) return plainMd.lexer(text || '')
  if (writtenByOwner) return mdOwnerText.lexer(text || '')
  return (literalTags ? mdLiteralTags : md).lexer(text || '')
}


/**
 * InlineMarkdown — one-line agent text such as a choice label: emphasis, code
 * and math only. `1. Fast` or `# Plan` stay text rather than becoming a list or
 * heading, tag-like text stays literal, and a link or image shows as its text,
 * so the result never holds a control and can sit inside one (a radio).
 */
export function InlineMarkdown({ text }) {
  const tokens = useMemo(
    () => textOnly(mdLiteralTags.Lexer.lexInline(text || '', mdLiteralTags.defaults)),
    [text],
  )
  return <InlineContent tokens={tokens} />
}

function textOnly(tokens) {
  return tokens.flatMap(token => {
    if (token.type === 'link') return textOnly(token.tokens || [])
    if (token.type === 'image') return token.text ? [{ type: 'text', text: token.text }] : []
    return token.tokens ? [{ ...token, tokens: textOnly(token.tokens) }] : [token]
  })
}


/**
 * ProgressiveMarkdown — active-answer mode.
 * Re-lexes on every update; only changed blocks re-render thanks to
 * React.memo comparison on token.raw. The same component stays mounted when
 * the active answer switches from its DB partial to live SSE data; streaming
 * affordances are props, not a second markdown subtree.
 */
export function ProgressiveMarkdown({
  text,
  markdownRange,
  isStreaming = false,
  onInternalNav,
  mediaDimensions,
  allowAppBlocks = false,
}) {
  // Counted because `text` grows by a few characters per reveal commit while
  // this re-tokenises the ENTIRE answer each time, making live streaming cost
  // grow with answer length. Labelled apart from the settled renderer below so
  // a report can tell "streaming is expensive" from "the transcript is
  // expensive" - those have different fixes.
  const tokens = useMemo(
    () => groupMarkdownImages(markdownRange ? markdownRangeTokens(markdownRange) : tokenize(text)),
    [text, markdownRange],
  )

  return (
    <>
      <div
        className="progressive-markdown md-blocks"
        data-is-streaming={isStreaming ? 'true' : undefined}
        aria-live={isStreaming ? 'polite' : undefined}
        aria-atomic={isStreaming ? 'false' : undefined}
      >
        {tokens.map((token, i) => {
          const appCard = appLinkCardFromParagraph(token, globalThis.window?.location?.href)
          if (appCard) {
            return <AppLinkCard key={i} card={appCard} onInternalNav={onInternalNav} />
          }
          if (token.type === 'blockKatex') {
            return <MathBlock key={i} tex={token.text} />
          }
          if (token.type === 'imageGallery') {
            return (
              <ImageGallery
                key={i}
                images={token.images}
                mediaDimensions={mediaDimensions}
              />
            )
          }
          return (
            <MemoBlock
              key={i}
              token={token}
              onInternalNav={onInternalNav}
              mediaDimensions={mediaDimensions}
              allowAppBlocks={allowAppBlocks}
            />
          )
        })}
        {isStreaming && (
          <span className="chat__cursor" aria-hidden="true" />
        )}
      </div>
    </>
  )
}


/**
 * StandardMarkdown — history mode.
 *
 * Settled blocks normally render once. A pathological cold transcript can
 * prepare one long block over several hidden frames; `renderFraction` reveals
 * token prefixes while one memoized token tree keeps already-prepared DOM.
 * `writtenByOwner` and `literalTags` select the rules described at
 * `configuredMarked`.
 * `math={false}` renders plain Markdown with no TeX (agent-written notes).
 */
export function StandardMarkdown({
  text,
  markdownRange,
  renderFraction,
  writtenByOwner = false,
  literalTags = false,
  onInternalNav,
  mediaDimensions,
  allowAppBlocks = false,
  math = true,
}) {
  // The settled-transcript renderer, so this is the one that matters for "a
  // stopped chat still feels slow". `useMemo` only holds while the component
  // stays mounted; if anything remounts messages this re-tokenises every
  // message, which the probe surfaces as a burst of calls with no stream
  // running.
  const tokens = useMemo(
    () => groupMarkdownImages(markdownRange
      ? markdownRangeTokens(markdownRange)
      : tokenize(text, { writtenByOwner, literalTags, math })),
    [text, markdownRange, writtenByOwner, literalTags, math],
  )
  const fraction = Number(renderFraction)
  const visibleTokens = Number.isFinite(fraction) && fraction > 0 && fraction < 1
    ? tokens.slice(0, Math.max(1, Math.ceil(tokens.length * fraction)))
    : tokens

  return (
    <div className="standard-markdown md-blocks">
      {visibleTokens.map((token, i) => {
        const appCard = appLinkCardFromParagraph(token, globalThis.window?.location?.href)
        if (appCard) {
          return <AppLinkCard key={i} card={appCard} onInternalNav={onInternalNav} />
        }
        if (token.type === 'blockKatex') {
          return <MathBlock key={i} tex={token.text} />
        }
        if (token.type === 'imageGallery') {
          return (
            <ImageGallery
              key={i}
              images={token.images}
              mediaDimensions={mediaDimensions}
            />
          )
        }
        return (
          <MemoBlock
            key={i}
            token={token}
            onInternalNav={onInternalNav}
            mediaDimensions={mediaDimensions}
            allowAppBlocks={allowAppBlocks}
          />
        )
      })}
    </div>
  )
}
