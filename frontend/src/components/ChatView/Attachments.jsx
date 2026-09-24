import { useEffect, useState } from 'react'
import { FileDocument } from '@openai/apps-sdk-ui/components/Icon'
import { BASE } from '../../api/client.js'
import { mediaTokenParam } from '../../api/mediaToken.js'
import ImagePreviewButton from './ImagePreviewButton.jsx'

export function generatedFileCanPreview(file) {
  return file?.kind === 'generated'
    && file.previewable === true
}

export function attachmentIsGalleryImage(file) {
  return file?.mime_type?.startsWith('image/')
    && (file.kind !== 'generated' || file.previewable === true)
}

export default function Attachments({ attachments, chatId }) {
  const hasAttachments = Array.isArray(attachments) && attachments.length > 0

  // Fetch a short-lived media token for this chat. Owner JWTs must not appear
  // in ?token= query params (they leak into access logs/history/Referer).
  const [tokenParam, setTokenParam] = useState(null)
  useEffect(() => {
    if (!hasAttachments) return undefined
    setTokenParam(null)
    let cancelled = false
    mediaTokenParam(chatId).then(p => {
      if (!cancelled) setTokenParam(p || null)
    })
    return () => { cancelled = true }
  }, [chatId, hasAttachments])

  if (!hasAttachments) return null
  const images = attachments.filter(attachmentIsGalleryImage)
  const files = attachments.filter(a => !attachmentIsGalleryImage(a))

  return (
    <div className="chat__attachments">
      {images.length > 0 && (
        <div className="chat__attach-images">
          {images.map((img, i) => (
            <AttachImage
              key={i}
              src={tokenParam
                ? `${BASE}/api/chats/${chatId}/${
                  img.kind === 'generated' ? 'generated-files' : 'uploads'
                }/${encodeURIComponent(img.name)}${tokenParam}${
                  img.kind === 'generated' ? '&preview=true' : ''
                }`
                : ''}
              alt={img.name}
            />
          ))}
        </div>
      )}
      {files.map((f, i) => {
        const isGenerated = f.kind === 'generated'
        const canPreview = generatedFileCanPreview(f)
        const href = tokenParam ? `${BASE}/api/chats/${chatId}/${
          isGenerated ? 'generated-files' : 'uploads'
        }/${encodeURIComponent(f.name)}${tokenParam}${canPreview ? '&preview=true' : ''}` : ''
        const content = (
          <>
            <FileDocument width={12} height={12} aria-hidden="true" />
            <span className="chat__attach-file-name">{f.name}</span>
            <span className="chat__attach-file-size">{Math.round(f.size / 1024)}KB</span>
          </>
        )
        if (!tokenParam) return isGenerated ? (
          <span key={i} className="chat__attach-file" aria-disabled="true">
            {content}
          </span>
        ) : null
        return (
          <a
            key={i}
            className="chat__attach-file"
            href={href}
            download={isGenerated && !canPreview ? f.name : undefined}
            target="_blank"
            rel="noopener noreferrer"
          >
            {content}
          </a>
        )
      })}
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
