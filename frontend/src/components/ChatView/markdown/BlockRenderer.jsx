import { useMemo } from 'react'
import { Marked } from 'marked'
import { MemoBlock, MathBlock } from './blocks.jsx'
import { mathTokens } from './mathTokens.js'
import { groupMarkdownImages } from './imageGallery.js'
import ImageGallery from './ImageGallery.jsx'
import AppLinkCard from './AppLinkCard.jsx'
import { appLinkCardFromParagraph } from './appLinkCard.js'
import { perfTime } from '../../../lib/perfProbe.js'
import '../markdown.css'

/**
 * Configured marked instance with math token support. Tokenization stays tiny
 * and synchronous; the renderer loads KaTeX only when a math token is present.
 */
const md = new Marked()
md.use(mathTokens())


function tokenize(text) {
  return md.lexer(text || '')
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
  isStreaming = false,
  onInternalNav,
  mediaDimensions,
}) {
  // Counted because `text` grows by a few characters per reveal commit while
  // this re-tokenises the ENTIRE answer each time, making live streaming cost
  // grow with answer length. Labelled apart from the settled renderer below so
  // a report can tell "streaming is expensive" from "the transcript is
  // expensive" - those have different fixes.
  const tokens = useMemo(
    () => perfTime('markdown.tokenize.streaming', () => groupMarkdownImages(tokenize(text))),
    [text],
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
          const appCard = appLinkCardFromParagraph(token, window.location.href)
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
 */
export function StandardMarkdown({
  text,
  renderFraction,
  onInternalNav,
  mediaDimensions,
}) {
  // The settled-transcript renderer, so this is the one that matters for "a
  // stopped chat still feels slow". `useMemo` only holds while the component
  // stays mounted; if anything remounts messages this re-tokenises every
  // message, which the probe surfaces as a burst of calls with no stream
  // running.
  const tokens = useMemo(
    () => perfTime('markdown.tokenize.settled', () => groupMarkdownImages(tokenize(text))),
    [text],
  )
  const fraction = Number(renderFraction)
  const visibleTokens = Number.isFinite(fraction) && fraction > 0 && fraction < 1
    ? tokens.slice(0, Math.max(1, Math.ceil(tokens.length * fraction)))
    : tokens

  return (
    <div className="standard-markdown md-blocks">
      {visibleTokens.map((token, i) => {
        const appCard = appLinkCardFromParagraph(token, window.location.href)
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
          />
        )
      })}
    </div>
  )
}
