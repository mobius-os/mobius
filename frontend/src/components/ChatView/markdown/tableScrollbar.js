/**
 * Geometry and pointer gesture of the drawn scrollbar under a chat table that
 * cannot fit (useTableScrollbar in blocks.jsx). The track spans the scroller's
 * full width, so track pixels map to table pixels by scrollWidth / trackWidth.
 */

/** Thumb size and position as CSS percentages: `widthPercent` of the track,
 * and `offsetPercent` of the thumb's own width, which is what translateX takes.
 * A table that fits gets a full-width thumb at the start. */
export function tableScrollbarThumb({ clientWidth, scrollWidth, scrollLeft }) {
  if (!(clientWidth > 0) || !(scrollWidth > clientWidth)) {
    return { widthPercent: 100, offsetPercent: 0 }
  }
  return {
    widthPercent: (clientWidth / scrollWidth) * 100,
    offsetPercent: (scrollLeft / clientWidth) * 100,
  }
}

/** The scrollLeft for a drag whose pointer is `distance` px along the track
 * from where it started, with the table at `startScrollLeft` then, clamped to
 * the table's scroll range. Measuring from the drag's start rather than adding
 * each move keeps the thumb under the pointer after it is held at either end. */
export function tableScrollLeftForDrag(
  startScrollLeft,
  distance,
  { clientWidth, scrollWidth, trackWidth },
) {
  if (!(trackWidth > 0)) return startScrollLeft
  const maxScroll = Math.max(scrollWidth - clientWidth, 0)
  const scrolled = startScrollLeft + distance * (scrollWidth / trackWidth)
  return Math.min(Math.max(scrolled, 0), maxScroll)
}

/** The scrollLeft that centres the thumb on a tap `offsetX` px into the
 * track, clamped to the table's scroll range. */
export function tableScrollLeftForTrackTap(offsetX, { clientWidth, scrollWidth, trackWidth }) {
  const maxScroll = Math.max(scrollWidth - clientWidth, 0)
  if (!(trackWidth > 0)) return 0
  const centred = offsetX * (scrollWidth / trackWidth) - clientWidth / 2
  return Math.min(Math.max(centred, 0), maxScroll)
}

/** Follows one pointer pressed on the track until it lifts, and returns the
 * AbortController that ends it early. A press outside the thumb jumps there;
 * from then on the table follows the pointer by the same proportion as the
 * thumb. The track is `touch-action: pan-y`, so a touch may turn out to be a
 * vertical swipe that the browser takes to scroll the chat: it cancels the
 * pointer, and the table goes back where it was. A touch therefore jumps only
 * once it moves or lifts; a mouse press jumps at once, as on a native bar. */
export function followTableScrollbarPointer(down, scroller, thumb) {
  const track = down.currentTarget
  const geometry = {
    clientWidth: scroller.clientWidth,
    scrollWidth: scroller.scrollWidth,
    trackWidth: track.clientWidth,
  }
  const startX = down.clientX
  const before = scroller.scrollLeft
  const thumbBox = thumb.getBoundingClientRect()
  const start = startX < thumbBox.left || startX > thumbBox.right
    ? tableScrollLeftForTrackTap(startX - track.getBoundingClientRect().left, geometry)
    : before
  if (down.pointerType === 'mouse') scroller.scrollLeft = start
  const follow = (event) => {
    scroller.scrollLeft = tableScrollLeftForDrag(start, event.clientX - startX, geometry)
  }
  const drag = new AbortController()
  const { signal } = drag
  track.setPointerCapture(down.pointerId)
  track.addEventListener('pointermove', follow, { signal })
  track.addEventListener('pointerup', follow, { signal })
  track.addEventListener('pointercancel', () => { scroller.scrollLeft = before }, { signal })
  // Capture ends after pointerup and pointercancel alike.
  track.addEventListener('lostpointercapture', () => drag.abort(), { signal })
  return drag
}
