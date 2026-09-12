/**
 * Serialize pin mutations in owner-intent order and expose one settlement
 * boundary to reordering. The settle loop follows work appended while it is
 * waiting, so a reorder can never overtake a pin/unpin that became visible
 * before the reorder request was built.
 */
export function createPinMutationQueue() {
  let tail = Promise.resolve()

  return {
    enqueue(mutation) {
      tail = tail.then(mutation, mutation)
      return tail
    },

    async settle() {
      let observed
      do {
        observed = tail
        try {
          await observed
        } catch {}
      } while (observed !== tail)
    },
  }
}
