/** Process-local registry of exact chat fixture IDs created by each worker. */
const createdChatIdsByWorker = new Map()

export function registerCreatedChats(workerIndex, chatsOrIds) {
  const values = Array.isArray(chatsOrIds) ? chatsOrIds : [chatsOrIds]
  let ids = createdChatIdsByWorker.get(workerIndex)
  if (!ids) {
    ids = new Set()
    createdChatIdsByWorker.set(workerIndex, ids)
  }
  for (const value of values) {
    const id = typeof value === 'string' ? value : value?.id
    if (id) ids.add(id)
  }
}

export function drainCreatedChats(workerIndex) {
  const ids = [...(createdChatIdsByWorker.get(workerIndex) || [])]
  createdChatIdsByWorker.delete(workerIndex)
  return ids
}
