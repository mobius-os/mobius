// Pure geometry for the drawer's pinned-row drag reorder. Given the pinned rows
// measured at drag start (top / height / center / key, in DOM order), the index
// of the row being dragged, and the pointer's vertical delta, it computes the
// whole live preview WITHOUT touching the DOM: where the row will land, how far
// each other row must slide to open the gap, where the lifted row rests, and the
// resulting top-to-bottom key order. Keeping this pure makes the fiddly
// index/offset math unit-testable; the imperative wiring in Drawer.jsx only
// applies the numbers as transforms.
//
// The gap is exactly the dragged row's own height, so every displaced row's
// previewed position equals its final natural position once the list commits —
// that identity is what lets the drop settle with no jump, regardless of whether
// rows have different heights (app rows are taller than chat rows).

export function computePinnedDrag(rows, fromIndex, deltaY) {
  const src = rows[fromIndex]
  const anchorTop = rows[0].top // top edge of the pinned region — a fixed anchor
  const others = rows.filter((_, i) => i !== fromIndex)
  const draggedCenter = src.center + deltaY

  // Insertion slot = how many other rows sit above the dragged row's center.
  let above = 0
  for (const o of others) if (o.center < draggedCenter) above += 1

  // Per-row gap shift. others[p] maps to original full index p (p < fromIndex)
  // or p + 1 (p >= fromIndex), so `p >= fromIndex` means it was below the source.
  const shifts = new Map()
  others.forEach((o, p) => {
    const wasBelowSource = p >= fromIndex
    let shift = 0
    if (wasBelowSource && p < above) shift = -src.height // rises into the vacated slot
    else if (!wasBelowSource && p >= above) shift = src.height // drops to open the gap
    shifts.set(o.key, shift)
  })

  // Resting offset for the lifted row: the top of its destination slot (the
  // anchor plus the heights of every row that ends up above it) minus its own
  // natural top.
  let stackedAbove = 0
  for (let p = 0; p < above; p += 1) stackedAbove += others[p].height
  const slotDelta = (anchorTop + stackedAbove) - src.top

  const finalKeys = others.map((o) => o.key)
  finalKeys.splice(above, 0, src.key)

  // Rows originally above the source number exactly `fromIndex`, so landing at
  // that same slot is a no-op.
  const changed = above !== fromIndex

  return { above, shifts, slotDelta, finalKeys, changed }
}
