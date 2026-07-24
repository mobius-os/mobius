import { useCallback, useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import ChevronLeft from 'lucide-react/dist/esm/icons/chevron-left.mjs'
import ChevronRight from 'lucide-react/dist/esm/icons/chevron-right.mjs'
import { ExpandableImage } from './InlineContent.jsx'
import ImageLightbox from './ImageLightbox.jsx'

const REDUCED_MOTION = '(prefers-reduced-motion: reduce)'

function smoothBehavior() {
  return window.matchMedia?.(REDUCED_MOTION).matches ? 'auto' : 'smooth'
}

/**
 * Compact, copy-friendly image filmstrip.
 *
 * mobius-ui:ImageRail — touch/trackpad scrolling is deliberately native. Do
 * not add pointer-move scroll emulation here: it removes mobile momentum and
 * competes with the chat's vertical scroll. Desktop buttons and Arrow keys are
 * the explicit non-gesture alternatives.
 */
export default function ImageGallery({ images }) {
  const count = images.length
  const railRef = useRef(null)
  const [canPrevious, setCanPrevious] = useState(false)
  const [canNext, setCanNext] = useState(false)
  const [viewerIndex, setViewerIndex] = useState(null)
  const [resolvedItems, setResolvedItems] = useState(() => (
    images.map(image => ({ src: null, alt: image.text || '' }))
  ))

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

  const registerResolved = useCallback((index, item) => {
    setResolvedItems(current => {
      if (
        current[index]?.src === item.src
        && current[index]?.alt === item.alt
      ) return current
      const next = [...current]
      next[index] = item
      return next
    })
  }, [])

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
    setViewerIndex(index)
  }, [registerResolved])

  const viewerItem = viewerIndex === null ? null : resolvedItems[viewerIndex]

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
      >
        {images.map((image, index) => (
          <div className="md-image-gallery__item" key={`${image.href}-${index}`}>
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
          onNavigate={setViewerIndex}
          onClose={() => setViewerIndex(null)}
        />,
        document.body,
      )}
    </section>
  )
}
