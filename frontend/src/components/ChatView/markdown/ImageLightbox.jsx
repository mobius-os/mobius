import { useRef, useEffect, useState, useCallback, useMemo } from 'react'
import { ChevronLeft, ChevronRight, Download, X } from '@openai/apps-sdk-ui/components/Icon'
import useDialogFocus from '../../../hooks/useDialogFocus.js'
import {
  clampImageScale,
  clampImageTransform,
  zoomImageAround,
} from './imageTransform.js'
import { gallerySwipeTarget } from './imageGallery.js'
import {
  captureLayoutSpace,
  clientDeltaToLayout,
  clientPointToLayout,
} from '../../../lib/layoutSpace.js'

function captureRootLayoutSpace() {
  return captureLayoutSpace(document.documentElement)
}

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
  const [paintedSrc, setPaintedSrc] = useState(activeSrc)
  const imageIsPending = paintedSrc !== activeSrc
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

  const toLayoutPoint = useCallback((x, y, space = captureRootLayoutSpace()) => clientPointToLayout(
    { x, y },
    space,
  ), [])

  const metrics = useCallback((space = captureRootLayoutSpace()) => {
    const img = imgRef.current
    const viewport = window.visualViewport
    const viewportSize = clientDeltaToLayout({
      x: viewport?.width || window.innerWidth,
      y: viewport?.height || window.innerHeight,
    }, space)
    return {
      baseWidth: img?.clientWidth || 0,
      baseHeight: img?.clientHeight || 0,
      viewportWidth: viewportSize.x,
      viewportHeight: viewportSize.y,
    }
  }, [])

  const baseCenter = useCallback((current = transformRef.current, space = captureRootLayoutSpace()) => {
    const rect = imgRef.current?.getBoundingClientRect()
    if (!rect) {
      const viewport = metrics(space)
      return { x: viewport.viewportWidth / 2, y: viewport.viewportHeight / 2 }
    }
    const paintedCenter = toLayoutPoint(
      rect.left + rect.width / 2,
      rect.top + rect.height / 2,
      space,
    )
    return {
      x: paintedCenter.x - current.x,
      y: paintedCenter.y - current.y,
    }
  }, [metrics, toLayoutPoint])

  const zoomAt = useCallback((nextScale, x, y, space = captureRootLayoutSpace()) => {
    setTransform((current) => zoomImageAround(
      current,
      nextScale,
      { x, y },
      baseCenter(current, space),
      metrics(space),
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

  const revealActiveImage = useCallback(async (event) => {
    const image = event.currentTarget
    try { await image.decode?.() } catch { /* the load event already confirmed a fallback */ }
    if (imgRef.current !== image) return
    setPaintedSrc(activeSrc)
  }, [activeSrc])

  const revealImageError = useCallback((event) => {
    if (imgRef.current !== event.currentTarget) return
    setPaintedSrc(activeSrc)
  }, [activeSrc])

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
    const space = captureRootLayoutSpace()
    const point = toLayoutPoint(event.clientX, event.clientY, space)
    zoomAt(nextScale, point.x, point.y, space)
  }, [toLayoutPoint, zoomAt])

  const toggleZoomAt = useCallback((x, y, space = captureRootLayoutSpace()) => {
    if (transformRef.current.scale > 1) reset()
    else zoomAt(2, x, y, space)
  }, [reset, zoomAt])

  const handleDoubleClick = useCallback((event) => {
    event.preventDefault()
    event.stopPropagation()
    const space = captureRootLayoutSpace()
    const point = toLayoutPoint(event.clientX, event.clientY, space)
    toggleZoomAt(point.x, point.y, space)
  }, [toLayoutPoint, toggleZoomAt])

  // Mouse/stylus drag-to-pan. Touch uses the pinch-aware handlers below.
  const handlePointerDown = useCallback((event) => {
    if (event.pointerType === 'touch' || event.button !== 0 || transformRef.current.scale <= 1) return
    event.currentTarget.setPointerCapture(event.pointerId)
    const point = toLayoutPoint(event.clientX, event.clientY, captureRootLayoutSpace())
    pointerPanRef.current = {
      id: event.pointerId,
      startX: point.x,
      startY: point.y,
      x: transformRef.current.x,
      y: transformRef.current.y,
    }
    setDragging(true)
    event.preventDefault()
  }, [toLayoutPoint])

  const handlePointerMove = useCallback((event) => {
    const pan = pointerPanRef.current
    if (!pan || pan.id !== event.pointerId) return
    const space = captureRootLayoutSpace()
    const point = toLayoutPoint(event.clientX, event.clientY, space)
    setTransform((current) => clampImageTransform({
      ...current,
      x: pan.x + point.x - pan.startX,
      y: pan.y + point.y - pan.startY,
    }, metrics(space)))
  }, [metrics, toLayoutPoint])

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

    const midpoint = (a, b, space) => toLayoutPoint(
      (a.clientX + b.clientX) / 2,
      (a.clientY + b.clientY) / 2,
      space,
    )
    const distance = (a, b) => Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY)

    const onTouchStart = (event) => {
      const current = transformRef.current
      const space = captureRootLayoutSpace()
      if (event.touches.length === 2) {
        const mid = midpoint(event.touches[0], event.touches[1], space)
        const center = baseCenter(current, space)
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
        const point = toLayoutPoint(touch.clientX, touch.clientY, space)
        tapStartRef.current = { x: touch.clientX, y: touch.clientY, moved: false }
        if (current.scale > 1) {
          panRef.current = { x: point.x - current.x, y: point.y - current.y }
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
        const space = captureRootLayoutSpace()
        const mid = midpoint(event.touches[0], event.touches[1], space)
        const scale = clampImageScale(pinch.scale * (distance(event.touches[0], event.touches[1]) / pinch.distance))
        setTransform(clampImageTransform({
          scale,
          x: mid.x - pinch.center.x - pinch.imageX * scale,
          y: mid.y - pinch.center.y - pinch.imageY * scale,
        }, metrics(space)))
      } else if (event.touches.length === 1 && panRef.current && transformRef.current.scale > 1) {
        event.preventDefault()
        const touch = event.touches[0]
        const pan = panRef.current
        const space = captureRootLayoutSpace()
        const point = toLayoutPoint(touch.clientX, touch.clientY, space)
        setTransform((current) => clampImageTransform({
          ...current,
          x: point.x - pan.x,
          y: point.y - pan.y,
        }, metrics(space)))
      }
    }

    const onTouchEnd = (event) => {
      if (event.touches.length === 1 && pinchRef.current) {
        const touch = event.touches[0]
        const current = transformRef.current
        const point = toLayoutPoint(touch.clientX, touch.clientY, captureRootLayoutSpace())
        panRef.current = { x: point.x - current.x, y: point.y - current.y }
      }
      if (event.touches.length === 0) {
        const swipe = swipeRef.current
        if (swipe) {
          const deltaX = swipe.x - swipe.startX
          const deltaY = swipe.y - swipe.startY
          const nextIndex = gallerySwipeTarget({
            deltaX, deltaY, index, items: galleryItems,
          })
          if (nextIndex !== null) goToIndex(nextIndex)
        }
        const tap = tapStartRef.current
        if (tap && !tap.moved) {
          const now = Date.now()
          const previous = lastTapRef.current
          if (previous && now - previous.time < 320 && Math.hypot(tap.x - previous.x, tap.y - previous.y) < 28) {
            const space = captureRootLayoutSpace()
            const point = toLayoutPoint(tap.x, tap.y, space)
            toggleZoomAt(point.x, point.y, space)
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
  }, [
    baseCenter,
    galleryItems,
    goToIndex,
    index,
    metrics,
    toggleZoomAt,
    toLayoutPoint,
  ])

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
    <div className="lightbox-overlay" role="presentation" onClick={onClose}>
      <div
        ref={dialogRef}
        className="lightbox-content"
        role="dialog"
        aria-modal="true"
        aria-label={hasGallery
          ? `Image ${index + 1} of ${galleryItems.length}${activeAlt ? `: ${activeAlt}` : ''}`
          : activeAlt || 'Image viewer'}
      >
        {imageIsPending && (
          <img
            key={paintedSrc}
            src={paintedSrc}
            alt=""
            className="lightbox-image lightbox-image--previous"
            aria-hidden="true"
            draggable={false}
          />
        )}
        <img
          key={activeSrc}
          ref={imgRef}
          src={activeSrc}
          alt={activeAlt}
          className={`lightbox-image${imageIsPending ? ' is-pending' : ''}${dragging ? ' is-dragging' : ''}`}
          onLoad={revealActiveImage}
          onError={revealImageError}
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
              <ChevronLeft width={22} height={22} aria-hidden="true" />
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
              <ChevronRight width={22} height={22} aria-hidden="true" />
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
              : <Download width={20} height={20} aria-hidden="true" />}
          </button>
          <button ref={closeBtnRef} className="lightbox-btn" onClick={onClose} title="Close" aria-label="Close">
            <X width={20} height={20} aria-hidden="true" />
          </button>
        </div>
      </div>
    </div>
  )
}
