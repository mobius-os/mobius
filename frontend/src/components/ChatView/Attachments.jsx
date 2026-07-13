import { useState, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { BASE } from '../../api/client.js'
import { mediaTokenParam } from '../../api/mediaToken.js'
import ImageLightbox from './markdown/ImageLightbox.jsx'

export default function Attachments({ attachments, chatId }) {
  if (!attachments || attachments.length === 0) return null
  const images = attachments.filter(a => a.mime_type?.startsWith('image/'))
  const files = attachments.filter(a => !a.mime_type?.startsWith('image/'))

  // Fetch a short-lived media token for this chat. Owner JWTs must not appear
  // in ?token= query params (they leak into access logs/history/Referer).
  const [tokenParam, setTokenParam] = useState('')
  useEffect(() => {
    let cancelled = false
    mediaTokenParam(chatId).then(p => {
      if (!cancelled) setTokenParam(p)
    })
    return () => { cancelled = true }
  }, [chatId])

  return (
    <div className="chat__attachments">
      {images.length > 0 && (
        <div className="chat__attach-images">
          {images.map((img, i) => (
            <AttachImage
              key={i}
              src={`${BASE}/api/chats/${chatId}/uploads/${encodeURIComponent(img.name)}${tokenParam}`}
              alt={img.name}
            />
          ))}
        </div>
      )}
      {files.map((f, i) => (
        <a
          key={i}
          className="chat__attach-file"
          href={`${BASE}/api/chats/${chatId}/uploads/${encodeURIComponent(f.name)}${tokenParam}`}
          target="_blank"
          rel="noopener noreferrer"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
          </svg>
          <span className="chat__attach-file-name">{f.name}</span>
          <span className="chat__attach-file-size">{Math.round(f.size / 1024)}KB</span>
        </a>
      ))}
    </div>
  )
}

function AttachImage({ src, alt }) {
  const [open, setOpen] = useState(false)
  // Don't render the image until we have a token (src would 403 without one).
  if (!src.includes('?token=')) return null
  return (
    <>
      <button
        type="button"
        className="chat__attach-thumb-button"
        aria-label={`Open ${alt || 'attached image'} preview`}
        onClick={() => setOpen(true)}
      >
        <img
          className="chat__attach-thumb"
          src={src}
          alt={alt}
          loading="lazy"
        />
      </button>
      {open && createPortal(
        <ImageLightbox src={src} alt={alt} onClose={() => setOpen(false)} />,
        document.body,
      )}
    </>
  )
}
