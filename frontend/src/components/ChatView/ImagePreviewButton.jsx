/* Shared trigger for the chat's zoomable image lightbox. */
import { useRef, useState } from 'react'
import ImageLightbox from './markdown/ImageLightbox.jsx'
import ChatPanePortal from './ChatPanePortal.jsx'
import { useHistoryDismiss } from '../../hooks/useHistoryDismiss.jsx'

export default function ImagePreviewButton({
  src,
  alt = '',
  buttonClassName,
  imageClassName,
  intrinsicWidth,
  intrinsicHeight,
  imageLoading = 'lazy',
  onPointerDown,
  onLoad,
  onError,
}) {
  const [open, setOpen] = useState(false)
  const buttonRef = useRef(null)
  const historyDismiss = useHistoryDismiss(() => setOpen(false))

  if (!src) return null

  return (
    <>
      <button
        ref={buttonRef}
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
          width={intrinsicWidth || undefined}
          height={intrinsicHeight || undefined}
          loading={imageLoading}
          draggable={false}
          onLoad={onLoad}
          onError={onError}
        />
      </button>
      {open && <ChatPanePortal anchorRef={buttonRef}>
        <ImageLightbox src={src} alt={alt} onClose={historyDismiss.close} />
      </ChatPanePortal>}
    </>
  )
}
