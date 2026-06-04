import { useState } from 'react'
import { createPortal } from 'react-dom'
import DOMPurify from 'dompurify'
import { getToken, BASE } from '../../../api/client.js'
import { renderInlineMath, renderBlockMath, renderMathToString } from './math.js'
import ImageLightbox from './ImageLightbox.jsx'
import '../lightbox.css'

const SAFE_LINK_PROTOCOLS = new Set(['http:', 'https:', 'mailto:'])
const SAFE_IMAGE_PROTOCOLS = new Set(['http:', 'https:'])

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
    const href = safeLinkHref(token.href)
    if (!href) {
      return <InlineContent tokens={token.tokens} />
    }
    return (
      <a
        href={href}
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

function safeUrl(href, protocols) {
  const cleaned = DOMPurify.sanitize(href || '').trim()
  if (!cleaned) return null
  try {
    const url = new URL(cleaned, location.origin)
    if (!protocols.has(url.protocol)) return null
    return cleaned
  } catch {
    return null
  }
}

function safeLinkHref(href) {
  return safeUrl(href, SAFE_LINK_PROTOCOLS)
}

function resolveImageSrc(href) {
  let src = safeUrl(href, SAFE_IMAGE_PROTOCOLS)
  if (!src) return null
  if (src.startsWith('/api/') || src.startsWith(BASE + '/api/')) {
    const url = new URL(src, location.origin)
    url.searchParams.set('token', getToken())
    src = url.pathname + url.search
  }
  return src
}

function ExpandableImage({ href, alt }) {
  const [open, setOpen] = useState(false)
  const [ratio, setRatio] = useState(null)
  const src = resolveImageSrc(href)
  if (!src) return null
  return (
    <>
      <span
        className="md-image-frame"
        style={ratio ? { '--md-image-ratio': ratio } : undefined}
      >
        <img
          src={src}
          alt={alt}
          className="md-image"
          onLoad={(e) => {
            const img = e.currentTarget
            if (img.naturalWidth && img.naturalHeight) {
              setRatio(`${img.naturalWidth} / ${img.naturalHeight}`)
            }
          }}
          onClick={() => setOpen(true)}
        />
      </span>
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
