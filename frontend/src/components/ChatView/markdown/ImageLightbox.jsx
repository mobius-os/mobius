import { useRef, useEffect, useState, useCallback } from 'react'

/**
 * Full-screen lightbox overlay with pinch-zoom, pan, mouse-wheel zoom,
 * double-tap reset, and download. Rendered via createPortal by the caller.
 */
export default function ImageLightbox({ src, alt, onClose }) {
  const [transform, setTransform] = useState({ scale: 1, x: 0, y: 0 })
  const imgRef = useRef(null)
  const pinchRef = useRef(null)
  const panRef = useRef(null)

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  // Pinch-to-zoom via touch events.
  useEffect(() => {
    const el = imgRef.current
    if (!el) return

    const onTouchStart = (e) => {
      if (e.touches.length === 2) {
        const dx = e.touches[0].clientX - e.touches[1].clientX
        const dy = e.touches[0].clientY - e.touches[1].clientY
        pinchRef.current = { dist: Math.hypot(dx, dy), scale: transform.scale }
      } else if (e.touches.length === 1 && transform.scale > 1) {
        panRef.current = {
          x: e.touches[0].clientX - transform.x,
          y: e.touches[0].clientY - transform.y,
        }
      }
    }

    const onTouchMove = (e) => {
      if (e.touches.length === 2 && pinchRef.current) {
        e.preventDefault()
        const dx = e.touches[0].clientX - e.touches[1].clientX
        const dy = e.touches[0].clientY - e.touches[1].clientY
        const dist = Math.hypot(dx, dy)
        const newScale = Math.min(5, Math.max(1, pinchRef.current.scale * (dist / pinchRef.current.dist)))
        setTransform((t) => ({ ...t, scale: newScale }))
      } else if (e.touches.length === 1 && panRef.current && transform.scale > 1) {
        e.preventDefault()
        setTransform((t) => ({
          ...t,
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
  }, [transform])

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

  const handleDownload = async () => {
    const resp = await fetch(src)
    const blob = await resp.blob()
    const urlPath = new URL(src, location.origin).pathname
    const filename = urlPath.split('/').pop() || 'image.png'
    const a = document.createElement('a')
    const objUrl = URL.createObjectURL(blob)
    a.href = objUrl
    a.download = filename
    a.click()
    setTimeout(() => URL.revokeObjectURL(objUrl), 1000)
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
          <button className="lightbox-btn" onClick={handleDownload} title="Save image" aria-label="Save image">
            <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
          </button>
          <button className="lightbox-btn" onClick={onClose} title="Close" aria-label="Close">
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
