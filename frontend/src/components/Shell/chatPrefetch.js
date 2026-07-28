export const RECENT_CHAT_PREFETCH_LIMIT = 2

export function recentChatsToPrefetch(chats, activeChatId) {
  return (Array.isArray(chats) ? chats : [])
    .filter(chat => (
      chat?.has_messages
      && String(chat.id) !== String(activeChatId)
    ))
    .sort((a, b) => String(b.activity_at || b.updated_at || '')
      .localeCompare(String(a.activity_at || a.updated_at || '')))
    .slice(0, RECENT_CHAT_PREFETCH_LIMIT)
}
