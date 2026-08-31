/* Project durable chat-app artifact rows into the compact composer model. */

const EMPTY_CHAT_APP_ARTIFACTS = Object.freeze([])

export function chatAppArtifactInvalidation(event) {
  if (
    event?.type === 'app_deleted'
    || event?.type === 'app_recovered'
    || event?.type === 'app_updated'
  ) {
    return { scope: 'all' }
  }
  if (event?.type === 'app_preview_ready' && event.chatId) {
    return { scope: 'chat', chatId: event.chatId }
  }
  return null
}

export function projectChatAppArtifacts(rows) {
  if (!Array.isArray(rows) || rows.length === 0) {
    return EMPTY_CHAT_APP_ARTIFACTS
  }
  return rows
    .filter(row => row?.app?.id != null && row?.touched_at)
    .map(row => ({
      ...row.app,
      chat_touched_at: row.touched_at,
      chat_seen_at: row.seen_at ?? null,
      has_unseen_chat_update: row.seen_at !== row.touched_at,
    }))
    .sort((left, right) => String(left.chat_touched_at).localeCompare(
      String(right.chat_touched_at),
    ))
}
