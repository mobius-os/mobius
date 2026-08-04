export const CHAT_PREFETCH_LIMIT = 6
export const RECENT_OPEN_CHAT_PREFETCH_LIMIT = 3

export function warmChatCandidates(
  chats, activeChatId, recentlyOpenedChatIds = [],
) {
  const activeId = String(activeChatId || '')
  const eligible = (Array.isArray(chats) ? chats : [])
    .filter(chat => chat?.has_messages && String(chat.id) !== activeId)
  const byId = new Map(eligible.map(chat => [String(chat.id), chat]))
  const selected = []
  const selectedIds = new Set()
  const add = (chat) => {
    if (!chat || selectedIds.has(String(chat.id))) return
    selected.push(chat)
    selectedIds.add(String(chat.id))
  }

  // Device-local open history catches the chats the owner actually moves
  // between, while server owner activity covers recent work from other devices.
  // Agent-only updated_at churn deliberately cannot crowd out either signal.
  const opened = (Array.isArray(recentlyOpenedChatIds)
    ? recentlyOpenedChatIds
    : [])
    .map(id => byId.get(String(id)))
    .filter(Boolean)
  opened.slice(0, RECENT_OPEN_CHAT_PREFETCH_LIMIT).forEach(add)

  eligible
    .filter(chat => chat.activity_at)
    .sort((a, b) => String(b.activity_at).localeCompare(String(a.activity_at)))
    .forEach((chat) => {
      if (selected.length < CHAT_PREFETCH_LIMIT) add(chat)
    })

  return selected.slice(0, CHAT_PREFETCH_LIMIT)
}
