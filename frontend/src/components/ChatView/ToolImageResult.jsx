/* Render viewed images through protected chat media or an exact inline result. */
import { useEffect, useState } from 'react'
import { BASE } from '../../api/client.js'
import { mediaTokenParam } from '../../api/mediaToken.js'
import ImagePreviewButton from './ImagePreviewButton.jsx'

export default function ToolImageResult({ reference }) {
  const [resolved, setResolved] = useState({
    reference: null,
    src: '',
    failed: false,
  })

  useEffect(() => {
    if (!reference) {
      setResolved({ reference: null, src: '', failed: false })
      return undefined
    }
    if (reference.kind === 'inline') {
      setResolved({ reference, src: reference.src, failed: false })
      return undefined
    }

    let cancelled = false
    setResolved({ reference, src: '', failed: false })
    mediaTokenParam(reference.chatId).then(param => {
      if (cancelled) return
      const path = `/api/chats/${encodeURIComponent(reference.chatId)}/${reference.collection}/`
        + encodeURIComponent(reference.filename)
      setResolved({
        reference,
        src: param ? `${BASE}${path}${param}` : '',
        failed: !param,
      })
    }).catch(() => {
      if (!cancelled) setResolved({ reference, src: '', failed: true })
    })
    return () => { cancelled = true }
  }, [reference])

  const current = resolved.reference === reference
    ? resolved
    : { src: '', failed: false }
  const alt = reference?.filename || 'Viewed image'

  if (current.failed) {
    return (
      <span className="chat__tool-image-status" role="status">
        Image preview unavailable
      </span>
    )
  }
  if (!current.src) {
    return (
      <span className="chat__tool-image-status" role="status">
        Loading image…
      </span>
    )
  }

  return (
    <ImagePreviewButton
      src={current.src}
      alt={alt}
      buttonClassName="chat__tool-image-button"
      imageClassName="chat__tool-image"
      onError={() => setResolved(current => (
        current.reference === reference
          ? { reference, src: '', failed: true }
          : current
      ))}
    />
  )
}
