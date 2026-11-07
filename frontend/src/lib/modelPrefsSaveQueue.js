/* Saves a shown list one request at a time, always sending the owner's latest intended value; a failure with nothing newer pending restores the last confirmed value. */

/**
 * `read()` returns the list currently shown, `show(list)` replaces it, and
 * `save(list)` resolves true when the server accepted that exact list.
 * `change(update)` applies `update` to the intended list, shows it at once
 * and queues a save. The queue owns the intended list while saves are in
 * flight: it reads the shown list only when idle, to start from what the
 * server last confirmed, so a refresh that replaces the shown list mid-queue
 * can never make the next save send a stale list. Changes made while a save is
 * in flight are folded into the next one. When the last save settles, the
 * shown list becomes what the server confirmed: the intended list on success,
 * or the previous confirmed list after a failure.
 */
export function createLatestSaveQueue({ read, show, save, onResult = () => {} }) {
  let saving = false
  let dirty = false
  let confirmed = null
  let intended = null

  async function flush() {
    if (saving) {
      dirty = true
      return
    }
    saving = true
    try {
      for (;;) {
        dirty = false
        const sending = intended
        let ok = false
        try {
          ok = await save(sending)
        } catch {
          ok = false
        }
        if (ok) confirmed = sending
        onResult(ok)
        if (dirty) continue
        intended = confirmed
        show(confirmed)
        return
      }
    } finally {
      saving = false
    }
  }

  return {
    change(update) {
      // While idle the shown list is exactly what the server last accepted.
      if (!saving) {
        confirmed = read()
        intended = confirmed
      }
      intended = update(intended)
      show(intended)
      return flush()
    },
  }
}
