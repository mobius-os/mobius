/**
 * Serialize every pin, unpin, and reorder in owner-intent order. Queueing the
 * complete operation—not only a preflight wait—also orders server commits and
 * response application when requests are delayed differently.
 */
export function createPinnedMutationQueue() {
  let tail = Promise.resolve()

  return {
    enqueue(mutation) {
      tail = tail.then(mutation, mutation)
      return tail
    },
  }
}
