/* Saves a shown list one request at a time, always sending the latest shown value; a failure with nothing newer pending restores the last confirmed value. */

/**
 * `read()` returns the list currently shown, `show(list)` replaces it, and
 * `save(list)` resolves true when the server accepted that exact list.
 * `change(update)` applies `update` to the shown list at once and queues a
 * save. Changes made while a save is in flight are folded into the next one,
 * so no toggle is lost and nothing sends a stale list. When a save fails and
 * nothing newer is waiting, the shown list returns to what the server last
 * confirmed; newer intent instead goes out with the next save.
 */
export function createLatestSaveQueue({ read, show, save, onResult = () => {} }) {
  let saving = false
  let dirty = false
  let confirmed = null

  async function flush() {
    if (saving) {
      dirty = true
      return
    }
    saving = true
    try {
      for (;;) {
        dirty = false
        const sending = read()
        let ok = false
        try {
          ok = await save(sending)
        } catch {
          ok = false
        }
        if (ok) confirmed = sending
        onResult(ok)
        if (dirty) continue
        if (!ok) show(confirmed)
        return
      }
    } finally {
      saving = false
    }
  }

  return {
    change(update) {
      // While idle the shown list is exactly what the server last accepted.
      if (!saving) confirmed = read()
      show(update(read()))
      return flush()
    },
  }
}
