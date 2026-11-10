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

/** How far the table scrolls when the pointer moves `distance` px along the
 * track, so the thumb stays under the pointer. */
export function tableScrollForTrackDistance(distance, { scrollWidth, trackWidth }) {
  return trackWidth > 0 ? distance * (scrollWidth / trackWidth) : 0
}

/** The scrollLeft that centres the thumb on a tap `offsetX` px into the
 * track, clamped to the table's scroll range. */
export function tableScrollLeftForTrackTap(offsetX, { clientWidth, scrollWidth, trackWidth }) {
  const maxScroll = Math.max(scrollWidth - clientWidth, 0)
  if (!(trackWidth > 0)) return 0
  const centred = offsetX * (scrollWidth / trackWidth) - clientWidth / 2
  return Math.min(Math.max(centred, 0), maxScroll)
}
