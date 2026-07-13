import { useMemo } from 'react'
import { Marked } from 'marked'
import markedKatex from 'marked-katex-extension'
import { MemoBlock, BlockToken, MathBlock } from './blocks.jsx'
import '../markdown.css'

/**
 * Configured marked instance with KaTeX math support.
 * The katex extension handles $...$ (inline) and $$...$$ (block)
 * natively in the tokenizer — no placeholder hacks needed.
 *
 * nonStandard:true relaxes spacing requirements around $ delimiters.
 * throwOnError:false prevents KaTeX parse errors from breaking render.
 */
const md = new Marked()
md.use(markedKatex({ nonStandard: true, throwOnError: false }))


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
export function ProgressiveMarkdown({ text, isStreaming = false }) {
  const tokens = useMemo(() => tokenize(text), [text])

  return (
    <>
      <div
        className="progressive-markdown md-blocks"
        data-is-streaming={isStreaming ? 'true' : undefined}
        aria-live={isStreaming ? 'polite' : undefined}
        aria-atomic={isStreaming ? 'false' : undefined}
      >
        {tokens.map((token, i) => {
          if (token.type === 'blockKatex') {
            return <MathBlock key={i} tex={token.text} />
          }
          if (token.type === 'space') return null
          return <MemoBlock key={i} token={token} />
        })}
      </div>
      {isStreaming && <span className="chat__cursor" />}
    </>
  )
}


/**
 * StandardMarkdown — history mode.
 * One-shot render, no memoization overhead.
 */
export function StandardMarkdown({ text }) {
  const tokens = useMemo(() => tokenize(text), [text])

  return (
    <div className="standard-markdown md-blocks">
      {tokens.map((token, i) => {
        if (token.type === 'blockKatex') {
          return <MathBlock key={i} tex={token.text} />
        }
        if (token.type === 'space') return null
        return <BlockToken key={i} token={token} />
      })}
    </div>
  )
}
