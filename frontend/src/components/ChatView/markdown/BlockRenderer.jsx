import { useMemo } from 'react'
import { MemoBlock, MathBlock } from './blocks.jsx'
import InlineContent from './InlineContent.jsx'
import { inlineTokens, tokenize } from './markdownParser.js'
import { groupMarkdownImages } from './imageGallery.js'
import ImageGallery from './ImageGallery.jsx'
import AppLinkCard from './AppLinkCard.jsx'
import { appLinkCardFromParagraph } from './appLinkCard.js'
import { markdownRangeTokens } from './steerMarkdownRange.js'
import '../markdown.css'

/**
 * InlineMarkdown — one-line agent text such as a choice label: emphasis, code
 * and math only. `1. Fast` or `# Plan` stay text rather than becoming a list or
 * heading, tag-like text stays literal, and a link or image shows as its text,
 * so the result never holds a control and can sit inside one (a radio).
 */
export function InlineMarkdown({ text }) {
  const tokens = useMemo(() => inlineTokens(text), [text])
  return <InlineContent tokens={tokens} />
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
 * `writtenByOwner` and `literalTags` select the rules described in
 * markdownParser.js.
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
