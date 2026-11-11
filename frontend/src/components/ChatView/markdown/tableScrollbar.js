/**
 * Geometry of the drawn scrollbar under a chat table that cannot fit
 * (useTableScrollbar in blocks.jsx). The track spans the scroller's full
 * width, so track pixels map to table pixels by scrollWidth / trackWidth.
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
