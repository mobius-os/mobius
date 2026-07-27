import { useEffect, useRef, useState, memo } from 'react'
import DOMPurify from 'dompurify'
import Check from 'lucide-react/dist/esm/icons/check.mjs'
import Copy from 'lucide-react/dist/esm/icons/copy.mjs'
import InlineContent from './InlineContent.jsx'
import { copyPlainText } from '../messageCopy.js'
import { useMathHtml } from './math.js'
import { highlightSync, highlightCode } from './highlight.js'

/**
 * Block-level markdown components.
 * Each handles its own overflow and styling.
 */

export function Paragraph({ token, onInternalNav, mediaDimensions }) {
  return (
    <p className="md-paragraph">
      <InlineContent
        tokens={token.tokens}
        onInternalNav={onInternalNav}
        mediaDimensions={mediaDimensions}
      />
    </p>
  )
}

export function Heading({ token, onInternalNav, mediaDimensions }) {
  const Tag = `h${token.depth}`
  return (
    <Tag className={`md-heading md-heading--${token.depth}`}>
      <InlineContent
        tokens={token.tokens}
        onInternalNav={onInternalNav}
        mediaDimensions={mediaDimensions}
      />
    </Tag>
  )
}

export function CodeBlock({ token }) {
  const lang = token.lang || ''
  const code = token.text || ''

  // Try synchronous highlight first (no reflow).
  // The first code block falls back to plain text briefly while the lazy
  // highlighter loads; later blocks usually highlight synchronously.
  const syncHtml = highlightSync(code, lang)
  const [asyncHtml, setAsyncHtml] = useState(null)

  // Copy affordance: the raw token text goes to the clipboard (never the
  // highlighted HTML), with a brief check-icon acknowledgement.
  const [copied, setCopied] = useState(false)
  const copyTimerRef = useRef(null)

  useEffect(() => {
    if (syncHtml) return  // already highlighted synchronously
    let cancelled = false
    highlightCode(code, lang).then(html => {
      if (!cancelled && html) setAsyncHtml(html)
    })
    return () => { cancelled = true }
  }, [code, lang, syncHtml])

  useEffect(() => () => clearTimeout(copyTimerRef.current), [])

  async function copyCode() {
    if (!(await copyPlainText(code))) return
    setCopied(true)
    clearTimeout(copyTimerRef.current)
    copyTimerRef.current = setTimeout(() => setCopied(false), 1600)
  }

  const html = syncHtml || asyncHtml

  return (
    <div className="md-code-wrap">
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
      <button
        type="button"
        className={`md-code-copy${copied ? ' md-code-copy--copied' : ''}`}
        onClick={copyCode}
        aria-label={copied ? 'Copied' : 'Copy code'}
        title="Copy code"
      >
        {copied
          ? <Check size={13} strokeWidth={2.3} aria-hidden="true" />
          : <Copy size={13} strokeWidth={2} aria-hidden="true" />}
      </button>
    </div>
  )
}

export function Table({ token, onInternalNav, mediaDimensions }) {
  return (
    <div className="md-table-wrap">
      <table className="md-table">
        <thead>
          <tr>
            {token.header.map((cell, i) => (
              <th key={i} style={token.align?.[i] ? { textAlign: token.align[i] } : undefined}>
                <InlineContent
                  tokens={cell.tokens}
                  onInternalNav={onInternalNav}
                  mediaDimensions={mediaDimensions}
                />
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {token.rows.map((row, i) => (
            <tr key={i}>
              {row.map((cell, j) => (
                <td key={j} style={token.align?.[j] ? { textAlign: token.align[j] } : undefined}>
                  <InlineContent
                    tokens={cell.tokens}
                    onInternalNav={onInternalNav}
                    mediaDimensions={mediaDimensions}
                  />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function BlockQuote({ token, onInternalNav, mediaDimensions }) {
  return (
    <blockquote className="md-blockquote">
      {token.tokens?.map((child, i) => (
        <BlockToken
          key={i}
          token={child}
          onInternalNav={onInternalNav}
          mediaDimensions={mediaDimensions}
        />
      ))}
    </blockquote>
  )
}

export function ListBlock({ token, onInternalNav, mediaDimensions }) {
  const Tag = token.ordered ? 'ol' : 'ul'
  return (
    <Tag className="md-list" start={token.ordered ? token.start : undefined}>
      {token.items.map((item, i) => (
        <li key={i} className="md-list-item">
          {item.tokens?.map((child, j) => {
            if (child.type === 'text' && child.tokens) {
              return (
                <InlineContent
                  key={j}
                  tokens={child.tokens}
                  onInternalNav={onInternalNav}
                  mediaDimensions={mediaDimensions}
                />
              )
            }
            return (
              <BlockToken
                key={j}
                token={child}
                onInternalNav={onInternalNav}
                mediaDimensions={mediaDimensions}
              />
            )
          })}
        </li>
      ))}
    </Tag>
  )
}

// KaTeX produces MathML elements — the same allow-list used in InlineContent.
// Keep the two in sync when bumping KaTeX.
const KATEX_PURIFY_CONFIG = {
  ADD_TAGS: ['math', 'mrow', 'mn', 'mo', 'mi', 'mspace', 'msup', 'msub',
             'msubsup', 'mfrac', 'msqrt', 'mroot', 'mtext', 'mstyle',
             'mover', 'munder', 'munderover', 'mtable', 'mtr', 'mtd',
             'menclose', 'mpadded', 'mphantom', 'semantics', 'annotation',
             'annotation-xml'],
  ADD_ATTR: ['xmlns', 'display', 'encoding', 'columnalign', 'mathvariant',
             'mathsize', 'stretchy', 'symmetric', 'lspace', 'rspace',
             'rowalign', 'columnspacing', 'rowspacing', 'width', 'height',
             'depth', 'voffset'],
  FORCE_BODY: true,
}

export function MathBlock({ tex }) {
  const html = useMathHtml(tex, true)
  if (html) {
    return <div className="md-math-block" dangerouslySetInnerHTML={{ __html: DOMPurify.sanitize(html, KATEX_PURIFY_CONFIG) }} />
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
export function BlockToken({ token, onInternalNav, mediaDimensions }) {
  switch (token.type) {
    case 'paragraph': return (
      <Paragraph
        token={token}
        onInternalNav={onInternalNav}
        mediaDimensions={mediaDimensions}
      />
    )
    case 'heading': return (
      <Heading
        token={token}
        onInternalNav={onInternalNav}
        mediaDimensions={mediaDimensions}
      />
    )
    case 'code': return <CodeBlock token={token} />
    case 'table': return (
      <Table
        token={token}
        onInternalNav={onInternalNav}
        mediaDimensions={mediaDimensions}
      />
    )
    case 'blockquote': return (
      <BlockQuote
        token={token}
        onInternalNav={onInternalNav}
        mediaDimensions={mediaDimensions}
      />
    )
    case 'list': return (
      <ListBlock
        token={token}
        onInternalNav={onInternalNav}
        mediaDimensions={mediaDimensions}
      />
    )
    case 'hr': return <HorizontalRule />
    case 'html': return null
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
    && prev.onInternalNav === next.onInternalNav
    && prev.mediaDimensions === next.mediaDimensions
})
