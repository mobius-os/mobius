import { useState, useEffect } from 'react'
import { createPortal } from 'react-dom'
import DOMPurify from 'dompurify'
import { getToken, BASE } from '../../../api/client.js'
import { mediaTokenParam } from '../../../api/mediaToken.js'
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

// Matches /api/chats/<chat_id>/{uploads,media,generated}/<file> (generated is the
// legacy alias of media). These paths require a short-lived media token on ?token=;
// the owner JWT must not appear there (it would leak into access logs, history,
// Referer). Other /api/ paths that appear in markdown images (rare) still get the
// owner token in the URL, but the primary media-serve paths are hardened.
const MEDIA_PATH_RE = /^(?:.*)?\/api\/chats\/([^/]+)\/(?:uploads|media|generated)\//

function getMediaChatId(src) {
  const m = src.match(MEDIA_PATH_RE)
  return m ? m[1] : null
}

function resolveStaticImageSrc(href) {
  // Returns a URL for non-media API paths (or null for invalid hrefs).
  // Appends the owner token for API paths that aren't upload/generated routes —
  // those use the async ExpandableImage path instead.
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
  const [imageVars, setImageVars] = useState(null)
  const [resolvedSrc, setResolvedSrc] = useState(null)

  const rawSrc = safeUrl(href, SAFE_IMAGE_PROTOCOLS)
  const mediaChatId = rawSrc ? getMediaChatId(rawSrc) : null

  useEffect(() => {
    if (!rawSrc) { setResolvedSrc(null); return }
    let cancelled = false
    if (mediaChatId) {
      // Media path: fetch a short-lived media token, never the owner JWT in URL.
      mediaTokenParam(mediaChatId).then(param => {
        if (!cancelled) setResolvedSrc(`${BASE}${new URL(rawSrc, location.origin).pathname}${param}`)
      })
    } else {
      // Non-media API path or external URL: use owner token (or no token for external).
      setResolvedSrc(resolveStaticImageSrc(rawSrc))
    }
    return () => { cancelled = true }
  }, [rawSrc, mediaChatId])

  if (!resolvedSrc) return null
  return (
    <>
      <span
        className="md-image-frame"
        style={imageVars || undefined}
      >
        <img
          src={resolvedSrc}
          alt={alt}
          className="md-image"
          onLoad={(e) => {
            const img = e.currentTarget
            if (img.naturalWidth && img.naturalHeight) {
              const ratio = img.naturalWidth / img.naturalHeight
              const viewportH =
                window.visualViewport?.height || window.innerHeight || 800
              const cappedH = Math.min(viewportH * 0.60, 480)
              const fitWidth = Math.min(
                520,
                Math.max(120, Math.round(cappedH * ratio)),
              )
              setImageVars({
                '--md-image-ratio': `${img.naturalWidth} / ${img.naturalHeight}`,
                '--md-image-fit-width': `${fitWidth}px`,
              })
            }
          }}
          onClick={() => setOpen(true)}
        />
      </span>
      {open && createPortal(
        <ImageLightbox src={resolvedSrc} alt={alt} onClose={() => setOpen(false)} />,
        document.body,
      )}
    </>
  )
}

// KaTeX produces MathML + SVG markup — DOMPurify supports both namespaces
// out of the box via ADD_TAGS / FORCE_BODY.  The config below is the
// minimal allow-list that lets KaTeX output survive without stripping its
// namespaced elements while blocking all non-math HTML injection paths.
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

function sanitizeKatex(html) {
  return DOMPurify.sanitize(html, KATEX_PURIFY_CONFIG)
}

function BlockMathDiv({ tex }) {
  // Synchronous render — no useEffect, no reflow.
  const html = renderMathToString(tex, true)
  if (html) {
    return <div className="md-math-block" dangerouslySetInnerHTML={{ __html: sanitizeKatex(html) }} />
  }
  return <div className="md-math-block">{tex}</div>
}

function InlineMathSpan({ tex }) {
  const html = renderMathToString(tex, false)
  if (html) {
    return <span className="md-math-inline" dangerouslySetInnerHTML={{ __html: sanitizeKatex(html) }} />
  }
  return <span className="md-math-inline">{tex}</span>
}
