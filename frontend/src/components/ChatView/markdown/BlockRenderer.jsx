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
 * Saved and live content share a tree so liveness changes preserve existing DOM.
 * Cold history can reveal token prefixes with renderFraction; owner text and
 * literal tags use the markdownParser rules, and math=false disables TeX.
 */
export function StandardMarkdown({
  text,
  markdownRange,
  isStreaming = false,
  renderFraction,
  writtenByOwner = false,
  literalTags = false,
  onInternalNav,
  mediaDimensions,
  allowAppBlocks = false,
  math = true,
}) {
  // Cursor and liveness changes reuse the already-tokenized content.
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
    <div
      className="standard-markdown md-blocks"
      data-is-streaming={isStreaming ? 'true' : undefined}
      aria-live={isStreaming ? 'polite' : undefined}
      aria-atomic={isStreaming ? 'false' : undefined}
    >
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
      {isStreaming && (
        <span className="chat__cursor" aria-hidden="true" />
      )}
    </div>
  )
}
