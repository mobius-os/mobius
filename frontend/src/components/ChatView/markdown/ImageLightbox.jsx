import { useRef, useEffect, useState, useCallback } from 'react'

/**
 * Full-screen lightbox overlay with pinch-zoom, pan, mouse-wheel zoom,
 * double-tap reset, and download. Rendered via createPortal by the caller.
 */
export default function ImageLightbox({ src, alt, onClose }) {
  const [transform, setTransform] = useState({ scale: 1, x: 0, y: 0 })
  // Keep a ref of the latest transform so touch handlers read it without
  // being listed as a dep — preventing the effect from re-registering on
  // every transform tick (which caused the listeners to be torn down and
  // re-attached every pinch/pan frame).
  const transformRef = useRef(transform)
  transformRef.current = transform

  const imgRef = useRef(null)
  const pinchRef = useRef(null)
  const panRef = useRef(null)
  const closeBtnRef = useRef(null)

  // Move focus to the close button on open; restore focus to the previously
  // focused element on close.
  useEffect(() => {
    const previously = document.activeElement
    closeBtnRef.current?.focus()
    return () => { previously?.focus?.() }
  }, [])

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Pinch-to-zoom via touch events.
  // Handlers read transform via transformRef so this effect only needs to
  // re-register when imgRef.current changes (i.e. never after mount).
  useEffect(() => {
    const el = imgRef.current
    if (!el) return

    const onTouchStart = (e) => {
      const t = transformRef.current
      if (e.touches.length === 2) {
        const dx = e.touches[0].clientX - e.touches[1].clientX
        const dy = e.touches[0].clientY - e.touches[1].clientY
        pinchRef.current = { dist: Math.hypot(dx, dy), scale: t.scale }
      } else if (e.touches.length === 1 && t.scale > 1) {
        panRef.current = {
          x: e.touches[0].clientX - t.x,
          y: e.touches[0].clientY - t.y,
        }
      }
    }

    const onTouchMove = (e) => {
      const t = transformRef.current
      if (e.touches.length === 2 && pinchRef.current) {
        e.preventDefault()
        const dx = e.touches[0].clientX - e.touches[1].clientX
        const dy = e.touches[0].clientY - e.touches[1].clientY
        const dist = Math.hypot(dx, dy)
        const newScale = Math.min(5, Math.max(1, pinchRef.current.scale * (dist / pinchRef.current.dist)))
        setTransform((cur) => ({ ...cur, scale: newScale }))
      } else if (e.touches.length === 1 && panRef.current && t.scale > 1) {
        e.preventDefault()
        setTransform((_cur) => ({
          ..._cur,
          x: e.touches[0].clientX - panRef.current.x,
          y: e.touches[0].clientY - panRef.current.y,
        }))
      }
    }

    const onTouchEnd = () => {
      pinchRef.current = null
      panRef.current = null
      setTransform((t) => t.scale <= 1 ? { scale: 1, x: 0, y: 0 } : t)
    }

    el.addEventListener('touchstart', onTouchStart, { passive: false })
    el.addEventListener('touchmove', onTouchMove, { passive: false })
    el.addEventListener('touchend', onTouchEnd)
    return () => {
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchmove', onTouchMove)
      el.removeEventListener('touchend', onTouchEnd)
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []) // intentionally empty — handlers read transform via transformRef

  // Mouse wheel zoom.
  const handleWheel = useCallback((e) => {
    e.preventDefault()
    setTransform((t) => {
      const newScale = Math.min(5, Math.max(1, t.scale - e.deltaY * 0.002))
      if (newScale <= 1) return { scale: 1, x: 0, y: 0 }
      return { ...t, scale: newScale }
    })
  }, [])

  // Double-tap to reset.
  const lastTap = useRef(0)
  const handleTap = useCallback(() => {
    const now = Date.now()
    if (now - lastTap.current < 300) {
      setTransform({ scale: 1, x: 0, y: 0 })
    }
    lastTap.current = now
  }, [])

  const [downloadError, setDownloadError] = useState(false)

  const handleDownload = async () => {
    setDownloadError(false)
    try {
      const resp = await fetch(src)
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
      const blob = await resp.blob()
      const urlPath = new URL(src, location.origin).pathname
      const filename = urlPath.split('/').pop() || 'image.png'
      const a = document.createElement('a')
      const objUrl = URL.createObjectURL(blob)
      a.href = objUrl
      a.download = filename
      a.click()
      setTimeout(() => URL.revokeObjectURL(objUrl), 1000)
    } catch {
      setDownloadError(true)
      // Reset the error label after 3 s so the button becomes usable again.
      setTimeout(() => setDownloadError(false), 3000)
    }
  }

  const handleOverlayClick = useCallback(() => {
    if (transform.scale > 1) {
      setTransform({ scale: 1, x: 0, y: 0 })
    } else {
      onClose()
    }
  }, [transform.scale, onClose])

  return (
    <div
      className="lightbox-overlay"
      onClick={handleOverlayClick}
      role="dialog"
      aria-modal="true"
      aria-label={alt || 'Image viewer'}
    >
      <div className="lightbox-content" onClick={(e) => e.stopPropagation()}>
        <img
          ref={imgRef}
          src={src}
          alt={alt}
          className="lightbox-image"
          onClick={handleTap}
          onWheel={handleWheel}
          style={{
            transform: `translate(${transform.x}px, ${transform.y}px) scale(${transform.scale})`,
            cursor: transform.scale > 1 ? 'grab' : 'zoom-in',
          }}
          draggable={false}
        />
        <div className="lightbox-actions">
          <button
            className="lightbox-btn"
            onClick={handleDownload}
            title={downloadError ? 'Download failed' : 'Save image'}
            aria-label={downloadError ? 'Download failed' : 'Save image'}
          >
            {downloadError ? (
              <span className="lightbox-dl-err" aria-live="assertive">!</span>
            ) : (
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>
                <polyline points="7 10 12 15 17 10"/>
                <line x1="12" y1="15" x2="12" y2="3"/>
              </svg>
            )}
          </button>
          <button ref={closeBtnRef} className="lightbox-btn" onClick={onClose} title="Close" aria-label="Close">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <line x1="18" y1="6" x2="6" y2="18"/>
              <line x1="6" y1="6" x2="18" y2="18"/>
            </svg>
          </button>
        </div>
      </div>
    </div>
  )
}
