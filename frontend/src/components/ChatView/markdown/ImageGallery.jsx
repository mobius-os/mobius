import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import ChevronLeft from 'lucide-react/dist/esm/icons/chevron-left.mjs'
import ChevronRight from 'lucide-react/dist/esm/icons/chevron-right.mjs'
import { ExpandableImage } from './InlineContent.jsx'
import ImageLightbox from './ImageLightbox.jsx'
import { projectResolvedGalleryItems } from './imageGallery.js'

const REDUCED_MOTION = '(prefers-reduced-motion: reduce)'

function smoothBehavior() {
  return window.matchMedia?.(REDUCED_MOTION).matches ? 'auto' : 'smooth'
}

/**
 * Compact, copy-friendly image filmstrip.
 *
 * mobius-ui:ImageRail — touch/trackpad scrolling is deliberately native so it
 * keeps momentum and cooperates with the chat's vertical scroll. Mouse and pen
 * get grab-to-scroll only after a horizontal gesture is established. Desktop
 * buttons and Arrow keys remain the explicit non-gesture alternatives.
 */
export default function ImageGallery({ images }) {
  const count = images.length
  const railRef = useRef(null)
  const dragRef = useRef(null)
  const suppressClickRef = useRef(false)
  const suppressTimerRef = useRef(0)
  const [canPrevious, setCanPrevious] = useState(false)
  const [canNext, setCanNext] = useState(false)
  const [viewerKey, setViewerKey] = useState(null)
  const [resolvedSources, setResolvedSources] = useState(() => new Map())
  const resolvedItems = useMemo(
    () => projectResolvedGalleryItems(images, resolvedSources),
    [images, resolvedSources],
  )

  const syncOverflow = useCallback(() => {
    const rail = railRef.current
    if (!rail) return
    const maxScroll = Math.max(0, rail.scrollWidth - rail.clientWidth)
    setCanPrevious(rail.scrollLeft > 2)
    setCanNext(rail.scrollLeft < maxScroll - 2)
  }, [])

  useEffect(() => {
    syncOverflow()
    const rail = railRef.current
    if (!rail || typeof ResizeObserver === 'undefined') return undefined
    const observer = new ResizeObserver(syncOverflow)
    observer.observe(rail)
    return () => observer.disconnect()
  }, [count, syncOverflow])

  const registerResolved = useCallback((_index, item) => {
    if (!item.href || !item.src) return
    setResolvedSources(current => {
      if (current.get(item.href) === item.src) return current
      const next = new Map(current)
      next.set(item.href, item.src)
      return next
    })
  }, [])

  // Progressive Markdown can replace or remove images while this component
  // remains mounted. Keep readiness attached to the image URL rather than its
  // transient array position, and release entries that left the current rail.
  useEffect(() => {
    const currentHrefs = new Set(images.map(image => image.href))
    setResolvedSources(current => {
      if ([...current.keys()].every(href => currentHrefs.has(href))) return current
      return new Map([...current].filter(([href]) => currentHrefs.has(href)))
    })
  }, [images])

  const scrollByItem = useCallback((direction) => {
    const rail = railRef.current
    const item = rail?.querySelector('.md-image-gallery__item')
    if (!rail || !item) return
    const gap = Number.parseFloat(getComputedStyle(rail).columnGap) || 0
    rail.scrollBy({
      left: direction * (item.getBoundingClientRect().width + gap),
      behavior: smoothBehavior(),
    })
  }, [])

  const handleKeyDown = useCallback((event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return
    event.preventDefault()
    scrollByItem(event.key === 'ArrowLeft' ? -1 : 1)
  }, [scrollByItem])

  const openViewer = useCallback((index, item) => {
    registerResolved(index, item)
    setViewerKey(resolvedItems[index]?.key || null)
  }, [registerResolved, resolvedItems])

  const viewerIndex = viewerKey === null
    ? -1
    : resolvedItems.findIndex(item => item.key === viewerKey)
  const viewerItem = viewerIndex < 0 ? null : resolvedItems[viewerIndex]

  useEffect(() => {
    if (viewerKey !== null && viewerIndex < 0) setViewerKey(null)
  }, [viewerIndex, viewerKey])

  useEffect(() => () => {
    clearTimeout(suppressTimerRef.current)
    dragRef.current = null
    suppressClickRef.current = false
  }, [])

  function beginPointerDrag(event) {
    if (!event.isPrimary || event.pointerType === 'touch') return
    if (event.pointerType === 'mouse' && event.button !== 0) return
    dragRef.current = {
      pointerId: event.pointerId,
      startX: event.clientX,
      startY: event.clientY,
      startScrollLeft: event.currentTarget.scrollLeft,
      axis: null,
      moved: false,
      captured: false,
    }
  }

  function movePointerDrag(event) {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    const deltaX = event.clientX - drag.startX
    const deltaY = event.clientY - drag.startY
    if (!drag.axis && Math.hypot(deltaX, deltaY) > 4) {
      drag.axis = Math.abs(deltaX) > Math.abs(deltaY) ? 'x' : 'y'
    }
    if (drag.axis !== 'x') return
    if (!drag.captured) {
      drag.captured = true
      event.currentTarget.setPointerCapture?.(event.pointerId)
      event.currentTarget.classList.add('is-dragging')
    }
    drag.moved = true
    event.preventDefault()
    event.currentTarget.scrollLeft = drag.startScrollLeft - deltaX
  }

  function endPointerDrag(event) {
    const drag = dragRef.current
    if (!drag || drag.pointerId !== event.pointerId) return
    dragRef.current = null
    event.currentTarget.classList.remove('is-dragging')
    if (event.currentTarget.hasPointerCapture?.(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId)
    }
    if (!drag.moved) return
    suppressClickRef.current = true
    clearTimeout(suppressTimerRef.current)
    suppressTimerRef.current = window.setTimeout(() => {
      suppressClickRef.current = false
    }, 0)
  }

  return (
    <section
      className={`md-image-gallery md-image-gallery--${Math.min(count, 3)}`}
      aria-label={`Related images, ${count} items`}
    >
      <div
        ref={railRef}
        className="md-image-gallery__rail"
        role="group"
        aria-label="Related images. Swipe or use arrow keys to browse."
        tabIndex={0}
        onScroll={syncOverflow}
        onKeyDown={handleKeyDown}
        onPointerDown={beginPointerDrag}
        onPointerMove={movePointerDrag}
        onPointerUp={endPointerDrag}
        onPointerCancel={endPointerDrag}
        onLostPointerCapture={endPointerDrag}
        onClickCapture={(event) => {
          if (!suppressClickRef.current) return
          suppressClickRef.current = false
          event.preventDefault()
          event.stopPropagation()
        }}
      >
        {images.map((image, index) => (
          <div className="md-image-gallery__item" key={resolvedItems[index].key}>
            <ExpandableImage
              href={image.href}
              alt={image.text || ''}
              imageIndex={index}
              loading={index === 0 ? 'eager' : 'lazy'}
              onResolved={registerResolved}
              onOpen={openViewer}
            />
          </div>
        ))}
      </div>

      <button
        type="button"
        className="md-image-gallery__nav md-image-gallery__nav--previous"
        aria-label="Previous images"
        disabled={!canPrevious}
        onClick={() => scrollByItem(-1)}
      >
        <ChevronLeft size={18} aria-hidden="true" />
      </button>
      <button
        type="button"
        className="md-image-gallery__nav md-image-gallery__nav--next"
        aria-label="Next images"
        disabled={!canNext}
        onClick={() => scrollByItem(1)}
      >
        <ChevronRight size={18} aria-hidden="true" />
      </button>

      {viewerItem?.src && createPortal(
        <ImageLightbox
          src={viewerItem.src}
          alt={viewerItem.alt}
          items={resolvedItems}
          index={viewerIndex}
          onNavigate={(nextIndex) => setViewerKey(resolvedItems[nextIndex]?.key || null)}
          onClose={() => setViewerKey(null)}
        />,
        document.body,
      )}
    </section>
  )
}
