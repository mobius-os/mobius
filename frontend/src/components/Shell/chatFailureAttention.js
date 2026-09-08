/* Durable drawer attention for hidden chat failures. */

export function failedChatIds(chats, visibleChatIds = new Set()) {
  const visible = new Set(
    Array.from(visibleChatIds || [], chatId => String(chatId)),
  )
  return new Set((chats || [])
    .filter(chat => (
      chat?.has_unseen_failure
      && !visible.has(String(chat.id))
    ))
    .map(chat => String(chat.id)))
}


export function withChatFailureSeen(
  chats,
  chatId,
  seenThroughVersion = Infinity,
) {
  if (!Array.isArray(chats)) return chats
  const id = String(chatId)
  const seenThrough = Number(seenThroughVersion)
  let changed = false
  const next = chats.map(chat => {
    if (String(chat?.id) !== id || !chat?.has_unseen_failure) return chat
    const rowVersion = Number(chat.unseen_failure_version)
    if (
      Number.isFinite(seenThrough)
      && Number.isFinite(rowVersion)
      && rowVersion > seenThrough
    ) return chat
    changed = true
    return {
      ...chat,
      has_unseen_failure: false,
      unseen_failure_version: null,
    }
  })
  return changed ? next : chats
}


export async function acknowledgeChatFailure({
  chatId,
  activityVersion,
  inFlight,
  request,
  clearCached,
  restoreServerTruth,
}) {
  const key = `${chatId}:${activityVersion}`
  if (inFlight.has(key)) return false
  inFlight.add(key)
  clearCached(chatId, activityVersion)
  try {
    const response = await request(chatId, activityVersion)
    if (!response?.ok) {
      throw new Error(
        `failure acknowledgement failed (${response?.status ?? 'unknown'})`,
      )
    }
    clearCached(chatId, activityVersion)
    return true
  } catch {
    try {
      await restoreServerTruth()
    } catch {
      // Reconnect and foreground refresh remain the durable recovery path.
    }
    return false
  } finally {
    inFlight.delete(key)
  }
}
