const runtimeSnapshotReads = new Map()

function runtimeSnapshotKey(principalKey, chatId) {
  return JSON.stringify([principalKey || 'unscoped', String(chatId)])
}

// A logical chat can have more than one physical ChatView during workspace
// handoff. Share only the small network read across those views; every caller
// still validates and applies the returned snapshot in its own lifecycle.
export function readChatRuntimeSnapshot(principalKey, chatId, read) {
  const key = runtimeSnapshotKey(principalKey, chatId)
  const current = runtimeSnapshotReads.get(key)
  if (current) return current.promise

  const owner = { invalidated: false }
  const retryIfInvalidated = (settled, rejected = false) => {
    if (owner.invalidated) {
      return readChatRuntimeSnapshot(principalKey, chatId, read)
    }
    if (rejected) throw settled
    return settled
  }
  owner.promise = Promise.resolve()
    .then(read)
    .then(
      value => retryIfInvalidated(value),
      error => retryIfInvalidated(error, true),
    )
    .finally(() => {
      if (runtimeSnapshotReads.get(key) === owner) {
        runtimeSnapshotReads.delete(key)
      }
    })
  runtimeSnapshotReads.set(key, owner)
  return owner.promise
}

// Fresh send and Stop are the two local generation boundaries. Detach any
// pre-mutation flight so the next reader starts fresh; existing consumers join
// that successor when their obsolete request settles.
export function invalidateChatRuntimeSnapshot(principalKey, chatId) {
  const key = runtimeSnapshotKey(principalKey, chatId)
  const current = runtimeSnapshotReads.get(key)
  if (!current) return
  current.invalidated = true
  runtimeSnapshotReads.delete(key)
}
