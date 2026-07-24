import { useRef, useEffect, useState, useCallback, useMemo } from 'react'
import ChevronLeft from 'lucide-react/dist/esm/icons/chevron-left.mjs'
import ChevronRight from 'lucide-react/dist/esm/icons/chevron-right.mjs'
import Download from 'lucide-react/dist/esm/icons/download.mjs'
import X from 'lucide-react/dist/esm/icons/x.mjs'
import useDialogFocus from '../../../hooks/useDialogFocus.js'
import {
  clampImageScale,
  clampImageTransform,
  zoomImageAround,
} from './imageTransform.js'
import { gallerySwipeTarget } from './imageGallery.js'

/**
 * Full-screen image viewer with pointer-centred wheel/pinch zoom, drag pan,
 * double-click/tap zoom, and optional gallery navigation. Rendered via
 * createPortal by the caller.
 */
export default function ImageLightbox({
  src,
  alt,
  items,
  index = 0,
  onNavigate,
  onClose,
}) {
  const [transform, setTransform] = useState({ scale: 1, x: 0, y: 0 })
  const [dragging, setDragging] = useState(false)
  const [downloadError, setDownloadError] = useState(false)

  const galleryItems = useMemo(
    () => (items?.length ? items : [{ src, alt }]),
    [alt, items, src],
  )
  const activeItem = galleryItems[index]?.src ? galleryItems[index] : { src, alt }
  const activeSrc = activeItem.src
  const activeAlt = activeItem.alt || ''
  const hasGallery = galleryItems.length > 1
  const canPrevious = hasGallery && index > 0 && !!galleryItems[index - 1]?.src
  const canNext = hasGallery
    && index < galleryItems.length - 1
    && !!galleryItems[index + 1]?.src

  const transformRef = useRef(transform)
  transformRef.current = transform
  const imgRef = useRef(null)
  const pinchRef = useRef(null)
  const panRef = useRef(null)
  const pointerPanRef = useRef(null)
  const swipeRef = useRef(null)
  const tapStartRef = useRef(null)
  const lastTapRef = useRef(null)
  const closeBtnRef = useRef(null)
  const dialogRef = useRef(null)
  const navigateRef = useRef(onNavigate)
  navigateRef.current = onNavigate

  useDialogFocus({
    containerRef: dialogRef,
    initialFocusRef: closeBtnRef,
    onClose,
  })

  const metrics = useCallback(() => {
    const img = imgRef.current
    const viewport = window.visualViewport
    return {
      baseWidth: img?.clientWidth || 0,
      baseHeight: img?.clientHeight || 0,
      viewportWidth: viewport?.width || window.innerWidth,
      viewportHeight: viewport?.height || window.innerHeight,
    }
  }, [])

  const baseCenter = useCallback((current = transformRef.current) => {
    const rect = imgRef.current?.getBoundingClientRect()
    if (!rect) {
      return { x: window.innerWidth / 2, y: window.innerHeight / 2 }
    }
    return {
      x: rect.left + rect.width / 2 - current.x,
      y: rect.top + rect.height / 2 - current.y,
    }
  }, [])

  const zoomAt = useCallback((nextScale, x, y) => {
    setTransform((current) => zoomImageAround(
      current,
      nextScale,
      { x, y },
      baseCenter(current),
      metrics(),
    ))
  }, [baseCenter, metrics])

  const reset = useCallback(() => {
    setTransform({ scale: 1, x: 0, y: 0 })
  }, [])

  const goToIndex = useCallback((nextIndex) => {
    if (!navigateRef.current || !galleryItems[nextIndex]?.src) return
    reset()
    navigateRef.current(nextIndex)
  }, [galleryItems, reset])

  useEffect(() => {
    reset()
  }, [activeSrc, reset])

  useEffect(() => {
    if (!hasGallery) return undefined
    const onKeyDown = (event) => {
      if (transformRef.current.scale > 1) return
      if (event.key === 'ArrowLeft' && canPrevious) {
        event.preventDefault()
        goToIndex(index - 1)
      } else if (event.key === 'ArrowRight' && canNext) {
        event.preventDefault()
        goToIndex(index + 1)
      }
    }
    document.addEventListener('keydown', onKeyDown, true)
    return () => document.removeEventListener('keydown', onKeyDown, true)
  }, [canNext, canPrevious, goToIndex, hasGallery, index])

  // Trackpad/mouse-wheel zoom follows the pointer rather than the image centre.
  const handleWheel = useCallback((event) => {
    event.preventDefault()
    const delta = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? window.innerHeight : 1)
    const nextScale = transformRef.current.scale * Math.exp(-delta * 0.0015)
    zoomAt(nextScale, event.clientX, event.clientY)
  }, [zoomAt])

  const toggleZoomAt = useCallback((x, y) => {
    if (transformRef.current.scale > 1) reset()
    else zoomAt(2, x, y)
  }, [reset, zoomAt])

  const handleDoubleClick = useCallback((event) => {
    event.preventDefault()
    event.stopPropagation()
    toggleZoomAt(event.clientX, event.clientY)
  }, [toggleZoomAt])

  // Mouse/stylus drag-to-pan. Touch uses the pinch-aware handlers below.
  const handlePointerDown = useCallback((event) => {
    if (event.pointerType === 'touch' || event.button !== 0 || transformRef.current.scale <= 1) return
    event.currentTarget.setPointerCapture(event.pointerId)
    pointerPanRef.current = {
      id: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      x: transformRef.current.x,
      y: transformRef.current.y,
    }
    setDragging(true)
    event.preventDefault()
  }, [])

  const handlePointerMove = useCallback((event) => {
    const pan = pointerPanRef.current
    if (!pan || pan.id !== event.pointerId) return
    setTransform((current) => clampImageTransform({
      ...current,
      x: pan.x + event.clientX - pan.startX,
      y: pan.y + event.clientY - pan.startY,
    }, metrics()))
  }, [metrics])

  const endPointerPan = useCallback((event) => {
    if (pointerPanRef.current?.id !== event.pointerId) return
    pointerPanRef.current = null
    setDragging(false)
    try { event.currentTarget.releasePointerCapture(event.pointerId) } catch { /* already released */ }
  }, [])

  // Native touch handling keeps two-finger pinch and one-finger pan coherent.
  useEffect(() => {
    const el = imgRef.current
    if (!el) return undefined

    const midpoint = (a, b) => ({ x: (a.clientX + b.clientX) / 2, y: (a.clientY + b.clientY) / 2 })
    const distance = (a, b) => Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY)

    const onTouchStart = (event) => {
      const current = transformRef.current
      if (event.touches.length === 2) {
        const mid = midpoint(event.touches[0], event.touches[1])
        const center = baseCenter(current)
        pinchRef.current = {
          distance: distance(event.touches[0], event.touches[1]),
          scale: current.scale,
          center,
          imageX: (mid.x - center.x - current.x) / current.scale,
          imageY: (mid.y - center.y - current.y) / current.scale,
        }
        panRef.current = null
        tapStartRef.current = null
      } else if (event.touches.length === 1) {
        const touch = event.touches[0]
        tapStartRef.current = { x: touch.clientX, y: touch.clientY, moved: false }
        if (current.scale > 1) {
          panRef.current = { x: touch.clientX - current.x, y: touch.clientY - current.y }
          swipeRef.current = null
        } else if (navigateRef.current) {
          swipeRef.current = {
            startX: touch.clientX,
            startY: touch.clientY,
            x: touch.clientX,
            y: touch.clientY,
          }
        }
      }
    }

    const onTouchMove = (event) => {
      if (tapStartRef.current && event.touches[0]) {
        if (swipeRef.current) {
          swipeRef.current.x = event.touches[0].clientX
          swipeRef.current.y = event.touches[0].clientY
        }
        const moved = Math.hypot(
          event.touches[0].clientX - tapStartRef.current.x,
          event.touches[0].clientY - tapStartRef.current.y,
        )
        if (moved > 8) tapStartRef.current.moved = true
      }

      if (event.touches.length === 2 && pinchRef.current) {
        event.preventDefault()
        const pinch = pinchRef.current
        const mid = midpoint(event.touches[0], event.touches[1])
        const scale = clampImageScale(pinch.scale * (distance(event.touches[0], event.touches[1]) / pinch.distance))
        setTransform(clampImageTransform({
          scale,
          x: mid.x - pinch.center.x - pinch.imageX * scale,
          y: mid.y - pinch.center.y - pinch.imageY * scale,
        }, metrics()))
      } else if (event.touches.length === 1 && panRef.current && transformRef.current.scale > 1) {
        event.preventDefault()
        const touch = event.touches[0]
        setTransform((current) => clampImageTransform({
          ...current,
          x: touch.clientX - panRef.current.x,
          y: touch.clientY - panRef.current.y,
        }, metrics()))
      }
    }

    const onTouchEnd = (event) => {
      if (event.touches.length === 1 && pinchRef.current) {
        const touch = event.touches[0]
        const current = transformRef.current
        panRef.current = { x: touch.clientX - current.x, y: touch.clientY - current.y }
      }
      if (event.touches.length === 0) {
        const swipe = swipeRef.current
        if (swipe) {
          const deltaX = swipe.x - swipe.startX
          const deltaY = swipe.y - swipe.startY
          const nextIndex = gallerySwipeTarget({
            deltaX, deltaY, index, items: galleryItems,
          })
          if (nextIndex !== null) navigateRef.current?.(nextIndex)
        }
        const tap = tapStartRef.current
        if (tap && !tap.moved) {
          const now = Date.now()
          const previous = lastTapRef.current
          if (previous && now - previous.time < 320 && Math.hypot(tap.x - previous.x, tap.y - previous.y) < 28) {
            toggleZoomAt(tap.x, tap.y)
            lastTapRef.current = null
          } else {
            lastTapRef.current = { x: tap.x, y: tap.y, time: now }
          }
        }
        pinchRef.current = null
        panRef.current = null
        swipeRef.current = null
        tapStartRef.current = null
      }
    }

    el.addEventListener('touchstart', onTouchStart, { passive: false })
    el.addEventListener('touchmove', onTouchMove, { passive: false })
    el.addEventListener('touchend', onTouchEnd)
    el.addEventListener('touchcancel', onTouchEnd)
    return () => {
      el.removeEventListener('touchstart', onTouchStart)
      el.removeEventListener('touchmove', onTouchMove)
      el.removeEventListener('touchend', onTouchEnd)
      el.removeEventListener('touchcancel', onTouchEnd)
    }
  }, [baseCenter, galleryItems, index, metrics, toggleZoomAt])

  // Keep the image reachable if the viewport changes while it is enlarged.
  useEffect(() => {
    const onResize = () => setTransform((current) => clampImageTransform(current, metrics()))
    window.addEventListener('resize', onResize)
    window.visualViewport?.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('resize', onResize)
      window.visualViewport?.removeEventListener('resize', onResize)
    }
  }, [metrics])

  const handleDownload = async () => {
    setDownloadError(false)
    try {
      const response = await fetch(activeSrc)
      if (!response.ok) throw new Error(`HTTP ${response.status}`)
      const blob = await response.blob()
      const urlPath = new URL(activeSrc, location.origin).pathname
      const filename = urlPath.split('/').pop() || 'image.png'
      const anchor = document.createElement('a')
      const objectUrl = URL.createObjectURL(blob)
      anchor.href = objectUrl
      anchor.download = filename
      anchor.click()
      setTimeout(() => URL.revokeObjectURL(objectUrl), 1000)
    } catch {
      setDownloadError(true)
      setTimeout(() => setDownloadError(false), 3000)
    }
  }

  return (
    <div className="lightbox-overlay" role="presentation">
      <div
        ref={dialogRef}
        className="lightbox-content"
        role="dialog"
        aria-modal="true"
        aria-label={hasGallery
          ? `Image ${index + 1} of ${galleryItems.length}${activeAlt ? `: ${activeAlt}` : ''}`
          : activeAlt || 'Image viewer'}
        onClick={onClose}
      >
        <img
          key={activeSrc}
          ref={imgRef}
          src={activeSrc}
          alt={activeAlt}
          className={`lightbox-image${dragging ? ' is-dragging' : ''}`}
          onClick={(event) => event.stopPropagation()}
          onDoubleClick={handleDoubleClick}
          onWheel={handleWheel}
          onPointerDown={handlePointerDown}
          onPointerMove={handlePointerMove}
          onPointerUp={endPointerPan}
          onPointerCancel={endPointerPan}
          style={{
            transform: `translate3d(${transform.x}px, ${transform.y}px, 0) scale(${transform.scale})`,
            cursor: dragging ? 'grabbing' : transform.scale > 1 ? 'grab' : 'zoom-in',
          }}
          draggable={false}
        />
        {hasGallery && (
          <>
            <div className="lightbox-count" aria-live="polite">
              {index + 1} / {galleryItems.length}
            </div>
            <button
              type="button"
              className="lightbox-nav lightbox-nav--previous"
              aria-label="Previous image"
              disabled={!canPrevious}
              onClick={(event) => {
                event.stopPropagation()
                goToIndex(index - 1)
              }}
            >
              <ChevronLeft size={22} aria-hidden="true" />
            </button>
            <button
              type="button"
              className="lightbox-nav lightbox-nav--next"
              aria-label="Next image"
              disabled={!canNext}
              onClick={(event) => {
                event.stopPropagation()
                goToIndex(index + 1)
              }}
            >
              <ChevronRight size={22} aria-hidden="true" />
            </button>
          </>
        )}
        <div className="lightbox-actions" onClick={(event) => event.stopPropagation()}>
          <button
            className="lightbox-btn"
            onClick={handleDownload}
            title={downloadError ? 'Download failed' : 'Save image'}
            aria-label={downloadError ? 'Download failed' : 'Save image'}
          >
            {downloadError
              ? <span className="lightbox-dl-err" aria-live="assertive">!</span>
              : <Download size={20} aria-hidden="true" />}
          </button>
          <button ref={closeBtnRef} className="lightbox-btn" onClick={onClose} title="Close" aria-label="Close">
            <X size={20} aria-hidden="true" />
          </button>
        </div>
      </div>
    </div>
  )
}
