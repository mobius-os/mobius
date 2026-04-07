import { useEffect, useState, memo } from 'react'
import DOMPurify from 'dompurify'
import InlineContent from './InlineContent.jsx'
import { renderBlockMath, renderMathToString } from './math.js'
import { highlightSync, highlightCode } from './highlight.js'

/**
 * Block-level markdown components.
 * Each handles its own overflow and styling.
 */

export function Paragraph({ token }) {
  return (
    <p className="md-paragraph">
      <InlineContent tokens={token.tokens} />
    </p>
  )
}

export function Heading({ token }) {
  const Tag = `h${token.depth}`
  return (
    <Tag className={`md-heading md-heading--${token.depth}`}>
      <InlineContent tokens={token.tokens} />
    </Tag>
  )
}

export function CodeBlock({ token }) {
  const lang = token.lang || ''
  const code = token.text || ''

  // Try synchronous highlight first (no reflow).
  // Falls back to async if hljs hasn't loaded yet (rare — only on
  // very fast page loads before the eager import completes).
  const syncHtml = highlightSync(code, lang)
  const [asyncHtml, setAsyncHtml] = useState(null)

  useEffect(() => {
    if (syncHtml) return  // already highlighted synchronously
    let cancelled = false
    highlightCode(code, lang).then(html => {
      if (!cancelled && html) setAsyncHtml(html)
    })
    return () => { cancelled = true }
  }, [code, lang, syncHtml])

  const html = syncHtml || asyncHtml

  return (
    <pre className="md-code-block">
      {lang && <span className="md-code-lang">{lang}</span>}
      {html ? (
        <code
          className={`md-code language-${lang}`}
          dangerouslySetInnerHTML={{
            __html: DOMPurify.sanitize(html),
          }}
        />
      ) : (
        <code className={`md-code language-${lang}`}>{code}</code>
      )}
    </pre>
  )
}

export function Table({ token }) {
  return (
    <div className="md-table-wrap">
      <table className="md-table">
        <thead>
          <tr>
            {token.header.map((cell, i) => (
              <th key={i} style={token.align?.[i] ? { textAlign: token.align[i] } : undefined}>
                <InlineContent tokens={cell.tokens} />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {token.rows.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j} style={token.align?.[j] ? { textAlign: token.align[j] } : undefined}>
                  <InlineContent tokens={cell.tokens} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function BlockQuote({ token }) {
  return (
    <blockquote className="md-blockquote">
      {token.tokens?.map((child, i) => (
        <BlockToken key={i} token={child} />
      ))}
    </blockquote>
  )
}

export function ListBlock({ token }) {
  const Tag = token.ordered ? 'ol' : 'ul'
  return (
    <Tag className="md-list" start={token.ordered ? token.start : undefined}>
      {token.items.map((item, i) => (
        <li key={i} className="md-list-item">
          {item.tokens?.map((child, j) => {
            if (child.type === 'text' && child.tokens) {
              return <InlineContent key={j} tokens={child.tokens} />
            }
            return <BlockToken key={j} token={child} />
          })}
        </li>
      ))}
    </Tag>
  )
}

export function MathBlock({ tex }) {
  const html = renderMathToString(tex, true)
  if (html) {
    return <div className="md-math-block" dangerouslySetInnerHTML={{ __html: html }} />
  }
  return <div className="md-math-block">{tex}</div>
}

export function HorizontalRule() {
  return <hr className="md-hr" />
}

/**
 * Renders a single block-level token.
 * Used by BlockQuote and other nesting containers.
 */
export function BlockToken({ token }) {
  switch (token.type) {
    case 'paragraph': return <Paragraph token={token} />
    case 'heading': return <Heading token={token} />
    case 'code': return <CodeBlock token={token} />
    case 'table': return <Table token={token} />
    case 'blockquote': return <BlockQuote token={token} />
    case 'list': return <ListBlock token={token} />
    case 'hr': return <HorizontalRule />
    case 'space': return null
    default: return token.raw ? <p className="md-paragraph">{token.raw}</p> : null
  }
}

/**
 * Memoized block wrapper for progressive (streaming) rendering.
 * Compares token raw text to decide if re-render is needed.
 */
export const MemoBlock = memo(BlockToken, (prev, next) => {
  return prev.token.raw === next.token.raw
})
