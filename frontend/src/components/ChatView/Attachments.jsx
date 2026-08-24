import { useEffect, useState } from 'react'
import { FileDocument } from '@openai/apps-sdk-ui/components/Icon'
import { BASE } from '../../api/client.js'
import { mediaTokenParam } from '../../api/mediaToken.js'
import ImagePreviewButton from './ImagePreviewButton.jsx'

export default function Attachments({ attachments, chatId }) {
  const hasAttachments = Array.isArray(attachments) && attachments.length > 0

  // Fetch a short-lived media token for this chat. Owner JWTs must not appear
  // in ?token= query params (they leak into access logs/history/Referer).
  const [tokenParam, setTokenParam] = useState('')
  useEffect(() => {
    if (!hasAttachments) return undefined
    let cancelled = false
    mediaTokenParam(chatId).then(p => {
      if (!cancelled) setTokenParam(p)
    })
    return () => { cancelled = true }
  }, [chatId, hasAttachments])

  if (!hasAttachments) return null
  const images = attachments.filter(a => a.mime_type?.startsWith('image/'))
  const files = attachments.filter(a => !a.mime_type?.startsWith('image/'))

  return (
    <div className="chat__attachments">
      {images.length > 0 && (
        <div className="chat__attach-images">
          {images.map((img, i) => (
            <AttachImage
              key={i}
              src={tokenParam
                ? `${BASE}/api/chats/${chatId}/uploads/${encodeURIComponent(img.name)}${tokenParam}`
                : ''}
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
          <FileDocument width={12} height={12} aria-hidden="true" />
          <span className="chat__attach-file-name">{f.name}</span>
          <span className="chat__attach-file-size">{Math.round(f.size / 1024)}KB</span>
        </a>
      ))}
    </div>
  )
}

function AttachImage({ src, alt }) {
  return (
    // Authorization controls when the image bytes can render, not when the
    // message gets its layout. Keeping this frame mounted transfers the fixed
    // attachment-card geometry from composer to transcript in one send paint.
    <span className="chat__attach-thumb-frame" aria-hidden={!src || undefined}>
      {src && (
        <ImagePreviewButton
          src={src}
          alt={alt || 'attached image'}
          buttonClassName="chat__attach-thumb-button"
          imageClassName="chat__attach-thumb"
        />
      )}
    </span>
  )
}
