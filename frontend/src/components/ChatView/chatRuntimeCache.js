function samePendingMessages(current, next) {
  if (current === next) return true
  if (!Array.isArray(current) || !Array.isArray(next)) return false
  if (current.length !== next.length) return false
  for (let i = 0; i < current.length; i += 1) {
    if (current[i] === next[i]) continue
    if (JSON.stringify(current[i]) !== JSON.stringify(next[i])) return false
  }
  return true
}

function runtimeFieldMatches(current, field, value) {
  if (field === 'pending_messages' || field === 'goal') {
    const currentValue = field === 'pending_messages'
      ? current?.pending_messages || []
      : current?.goal || null
    const nextValue = field === 'pending_messages' ? value || [] : value || null
    if (currentValue === nextValue) return true
    return JSON.stringify(currentValue) === JSON.stringify(nextValue)
  }
  // Waits arrive as a fresh array every response, so a reference compare
  // would defeat the skip guard and persist the whole chat again.
  if (field === 'waits') {
    const currentValue = current?.waits || []
    const nextValue = value || []
    if (currentValue === nextValue) return true
    return JSON.stringify(currentValue) === JSON.stringify(nextValue)
  }
  return current?.[field] === value
}

/**
 * Patch the persisted chat cache only when a runtime field genuinely changes.
 *
 * TanStack Query publishes an `updated` event even when setQueryData's updater
 * returns the existing object. The IndexedDB persister listens to that event
 * and serializes the whole persisted cache, including chat transcripts. The
 * runtime fallback poll therefore has to skip setQueryData itself—not merely
 * rely on structural sharing—when the server repeats the same state.
 */
export function updateChatRuntimeCache(queryClient, queryKey, patch) {
  const fields = Object.keys(patch)
  const current = queryClient.getQueryData(queryKey)
  if (
    current
    && fields.every(field => runtimeFieldMatches(current, field, patch[field]))
  ) {
    return
  }

  queryClient.setQueryData(queryKey, existing => ({
    ...(existing || {}),
    ...patch,
  }))
}
