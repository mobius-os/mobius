/* Shared trigger for the chat's zoomable image lightbox. */
import { useState } from 'react'
import { createPortal } from 'react-dom'
import ImageLightbox from './markdown/ImageLightbox.jsx'
import { useHistoryDismiss } from '../../hooks/useHistoryDismiss.jsx'

export default function ImagePreviewButton({
  src,
  alt = '',
  buttonClassName,
  imageClassName,
  onPointerDown,
  onError,
}) {
  const [open, setOpen] = useState(false)
  const historyDismiss = useHistoryDismiss(() => setOpen(false))

  if (!src) return null

  return (
    <>
      <button
        type="button"
        className={buttonClassName}
        aria-label={`Open ${alt || 'image'} preview`}
        onPointerDown={onPointerDown}
        onClick={() => {
          historyDismiss.open()
          setOpen(true)
        }}
      >
        <img
          className={imageClassName}
          src={src}
          alt={alt}
          loading="lazy"
          draggable={false}
          onError={onError}
        />
      </button>
      {open && createPortal(
        <ImageLightbox src={src} alt={alt} onClose={historyDismiss.close} />,
        document.body,
      )}
    </>
  )
}
