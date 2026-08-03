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

// The drag preview is imperative while the durable list order arrives through
// React Query on its next notification task. Clearing transforms before React
// has moved the keyed rows exposes the old order for one paint. Classify that
// handoff from the rendered keys themselves: an exact match is committed, the
// same keys in another order are still pending, and a changed key set means a
// concurrent pin/unpin/delete superseded this drag.
export function pinnedOrderHandoffStatus(currentKeys, expectedKeys) {
  if (!Array.isArray(currentKeys) || !Array.isArray(expectedKeys)) {
    return 'superseded'
  }
  if (
    currentKeys.length === expectedKeys.length
    && currentKeys.every((key, index) => key === expectedKeys[index])
  ) return 'committed'
  if (currentKeys.length !== expectedKeys.length) return 'superseded'

  const current = new Set(currentKeys)
  const expected = new Set(expectedKeys)
  if (
    current.size !== currentKeys.length
    || expected.size !== expectedKeys.length
  ) return 'superseded'
  return currentKeys.every(key => expected.has(key)) ? 'pending' : 'superseded'
}

function pinnedEntryKey(entry) {
  return `${entry.kind}:${entry.item.id}`
}

// While persistence is in flight, this projection is the visible authority for
// the combined pinned section. Underlying chat/app queries may refresh on
// different tasks; as long as they still contain the same identities, keep the
// order the owner chose instead of painting mixed timestamp snapshots.
export function projectPinnedEntries(entries, orderedKeys) {
  const currentKeys = entries.map(pinnedEntryKey)
  if (pinnedOrderHandoffStatus(currentKeys, orderedKeys) === 'superseded') {
    return entries
  }
  const byKey = new Map(entries.map(entry => [pinnedEntryKey(entry), entry]))
  return orderedKeys.map(key => byKey.get(key))
}

// The handoff can retire only after BOTH query observers expose the exact ranks
// returned by the atomic save. Matching order alone is insufficient: one query
// may still hold client timestamps that happen to sort correctly until the
// other query refreshes and briefly interleaves against them.
export function pinnedEntriesMatchRanks(entries, expectedRanks) {
  if (!Array.isArray(expectedRanks) || entries.length !== expectedRanks.length) {
    return false
  }
  const current = new Map(entries.map(entry => [
    pinnedEntryKey(entry),
    String(entry.item.pinned_at || ''),
  ]))
  return expectedRanks.every(({ key, pinnedAt }) => (
    current.get(key) === String(pinnedAt || '')
  ))
}

/**
 * Hold the preview transforms until the keyed DOM order has committed.
 * MutationObserver fires after React's DOM move and before the browser paints,
 * so the caller can clear transforms without revealing the old natural order.
 */
export function observePinnedOrderHandoff(
  root,
  expectedKeys,
  onSettled,
  MutationObserverImpl = globalThis.MutationObserver,
) {
  let observer = null
  let settled = false

  const renderedKeys = () => (
    root && typeof root.querySelectorAll === 'function'
      ? [...root.querySelectorAll('[data-pinned-key]')]
        .map(node => node.dataset?.pinnedKey)
      : []
  )
  const settleIfReady = () => {
    if (settled) return false
    const status = pinnedOrderHandoffStatus(renderedKeys(), expectedKeys)
    if (status === 'pending') return false
    settled = true
    observer?.disconnect()
    onSettled(status)
    return true
  }

  if (!settleIfReady()) {
    if (typeof MutationObserverImpl !== 'function') {
      settled = true
      onSettled('superseded')
    } else {
      observer = new MutationObserverImpl(settleIfReady)
      observer.observe(root, { childList: true, subtree: false })
      // Close the tiny check→observe race if React committed between reads.
      settleIfReady()
    }
  }

  return () => {
    if (settled) return
    settled = true
    observer?.disconnect()
  }
}
