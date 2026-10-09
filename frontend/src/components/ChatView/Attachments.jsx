import { useEffect, useState } from 'react'
import { FileDocument } from '@openai/apps-sdk-ui/components/Icon'
import { BASE } from '../../api/client.js'
import { mediaTokenParam } from '../../api/mediaToken.js'
import ImagePreviewButton from './ImagePreviewButton.jsx'
import DocumentAttachment from './DocumentAttachment.jsx'
import { ExpandableImage } from './markdown/InlineContent.jsx'

export function generatedFileCanPreview(file) {
  return file?.kind === 'generated'
    && file.previewable === true
}

export function attachmentIsGalleryImage(file) {
  return file?.mime_type?.startsWith('image/')
    && (file.kind !== 'generated' || file.previewable === true)
}

export function generatedFileIsMarkdown(file) {
  return file?.kind === 'generated' && file.mime_type === 'text/markdown'
}

export function generatedFileIsPdf(file) {
  return generatedFileCanPreview(file) && file.mime_type === 'application/pdf'
}

export function documentAttachmentIdentity(file, chatId) {
  return `${chatId}:${file.name}:${file.sha256 || ''}`
}

export default function Attachments({ attachments, chatId, mediaDimensions }) {
  const hasAttachments = Array.isArray(attachments) && attachments.length > 0

  const needsToken = hasAttachments && attachments.some(file =>
    file.kind !== 'generated' || !attachmentIsGalleryImage(file),
  )

  // Fetch a short-lived media token for this chat. Owner JWTs must not appear
  // in ?token= query params (they leak into access logs/history/Referer).
  const [tokenParam, setTokenParam] = useState(null)
  const [expandedNames, setExpandedNames] = useState(() => new Set())
  useEffect(() => {
    if (!needsToken) return undefined
    setTokenParam(null)
    let cancelled = false
    mediaTokenParam(chatId).then(p => {
      if (!cancelled) setTokenParam(p || null)
    })
    return () => { cancelled = true }
  }, [chatId, needsToken])

  if (!hasAttachments) return null
  const images = attachments.filter(attachmentIsGalleryImage)
  const files = attachments.filter(a => !attachmentIsGalleryImage(a))
  const hasDocuments = files.some(f => generatedFileIsMarkdown(f) || generatedFileIsPdf(f))

  return (
    <div className={`chat__attachments${hasDocuments ? ' chat__attachments--documents' : ''}`}>
      {images.length > 0 && (
        <div className="chat__attach-images">
          {images.map((img, i) => img.kind === 'generated' ? (
            <span key={img.name} className="chat__generated-image">
              <ExpandableImage
                href={`/api/chats/${encodeURIComponent(chatId)}/generated-files/${encodeURIComponent(img.name)}`}
                alt={img.name}
                loading="eager"
                mediaDimensions={mediaDimensions}
              />
            </span>
          ) : (
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
      {files.length > 0 && <div className="chat__attach-files">{files.map((f, i) => {
        const isGenerated = f.kind === 'generated'
        const isMarkdown = generatedFileIsMarkdown(f)
        const hasChatPreview = isMarkdown || generatedFileIsPdf(f)
        const identity = documentAttachmentIdentity(f, chatId)
        const previewOpen = expandedNames.has(identity)
        const canPreview = generatedFileCanPreview(f)
        const href = tokenParam ? `${BASE}/api/chats/${encodeURIComponent(chatId)}/${
          isGenerated ? 'generated-files' : 'uploads'
        }/${encodeURIComponent(f.name)}${tokenParam}${canPreview && !hasChatPreview ? '&preview=true' : ''}` : ''
        const content = (
          <>
            <FileDocument width={12} height={12} aria-hidden="true" />
            <span className="chat__attach-file-name">{f.name}</span>
            <span className="chat__attach-file-size">{Math.round(f.size / 1024)}KB</span>
          </>
        )
        if (hasChatPreview) return <DocumentAttachment
          key={identity}
          file={f}
          chatId={chatId}
          expanded={previewOpen}
          onToggle={() => setExpandedNames(current => {
            const next = new Set(current)
            if (next.has(identity)) next.delete(identity)
            else next.add(identity)
            return next
          })}
        />
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
      })}</div>}
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
