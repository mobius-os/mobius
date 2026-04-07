import { useState } from 'react'
import { createPortal } from 'react-dom'
import DOMPurify from 'dompurify'
import { getToken } from '../../../api/client.js'
import { renderInlineMath, renderBlockMath, renderMathToString } from './math.js'
import ImageLightbox from './ImageLightbox.jsx'
import '../lightbox.css'

/**
 * Renders inline markdown tokens (text, bold, italic, code, links, math).
 * Takes a marked inline token array and produces React elements.
 */
export default function InlineContent({ tokens }) {
  if (!tokens) return null
  return tokens.map((token, i) => <InlineToken key={i} token={token} />)
}

function InlineToken({ token }) {
  if (token.type === 'text') {
    return token.text
  }

  // Math from marked-katex-extension.
  // displayMode:true means block-style math ($$...$$) that happened
  // to be on one line — render as block.  displayMode:false is inline.
  if (token.type === 'inlineKatex') {
    if (token.displayMode) {
      return <BlockMathDiv tex={token.text} />
    }
    return <InlineMathSpan tex={token.text} />
  }

  if (token.type === 'strong') {
    return <strong><InlineContent tokens={token.tokens} /></strong>
  }

  if (token.type === 'em') {
    return <em><InlineContent tokens={token.tokens} /></em>
  }

  if (token.type === 'codespan') {
    return <code className="md-inline-code">{token.text}</code>
  }

  if (token.type === 'link') {
    return (
      <a
        href={DOMPurify.sanitize(token.href)}
        target="_blank"
        rel="noopener noreferrer"
        title={token.title || undefined}
      >
        <InlineContent tokens={token.tokens} />
      </a>
    )
  }

  if (token.type === 'image') {
    return <ExpandableImage href={token.href} alt={token.text || ''} />
  }

  if (token.type === 'br') {
    return <br />
  }

  if (token.type === 'del') {
    return <del><InlineContent tokens={token.tokens} /></del>
  }

  // Suppress any stray HTML tokens to avoid tag leakage.
  if (token.type === 'html') {
    return null
  }

  // Fallback: render raw text.
  return token.raw || token.text || ''
}

function resolveImageSrc(href) {
  let src = DOMPurify.sanitize(href)
  if (src.startsWith('/api/')) {
    const url = new URL(src, location.origin)
    url.searchParams.set('token', getToken())
    src = url.pathname + url.search
  }
  return src
}

function ExpandableImage({ href, alt }) {
  const [open, setOpen] = useState(false)
  const src = resolveImageSrc(href)
  return (
    <>
      <img
        src={src}
        alt={alt}
        className="md-image"
        onClick={() => setOpen(true)}
      />
      {open && createPortal(
        <ImageLightbox src={src} alt={alt} onClose={() => setOpen(false)} />,
        document.body,
      )}
    </>
  )
}

function BlockMathDiv({ tex }) {
  // Synchronous render — no useEffect, no reflow.
  const html = renderMathToString(tex, true)
  if (html) {
    return <div className="md-math-block" dangerouslySetInnerHTML={{ __html: html }} />
  }
  return <div className="md-math-block">{tex}</div>
}

function InlineMathSpan({ tex }) {
  const html = renderMathToString(tex, false)
  if (html) {
    return <span className="md-math-inline" dangerouslySetInnerHTML={{ __html: html }} />
  }
  return <span className="md-math-inline">{tex}</span>
}
