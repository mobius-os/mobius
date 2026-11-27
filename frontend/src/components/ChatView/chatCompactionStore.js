// Which chats the server is compacting. The server owns this window: chat
// reads report it as `compacting` and the system stream announces each edge
// as `chat_compaction_changed`. Keeping it in a module store rather than one
// ChatView's state means a remounted pane, a reloaded tab, or another device
// still shows that the chat is busy and why a new message is waiting.

const compactingChats = new Map()
const listeners = new Set()

function emit() {
  for (const listener of listeners) listener()
}

export function setChatCompacting(chatId, kind) {
  if (chatId === null || chatId === undefined || chatId === '') return
  const key = String(chatId)
  const next = kind || null
  if ((compactingChats.get(key) || null) === next) return
  if (next) compactingChats.set(key, next)
  else compactingChats.delete(key)
  emit()
}

export function chatCompactingKind(chatId) {
  if (chatId === null || chatId === undefined) return null
  return compactingChats.get(String(chatId)) || null
}

export function subscribeChatCompaction(listener) {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

// Tells every view of the chat why a new message is waiting.
export function compactionNotice(kind) {
  if (!kind) return null
  return 'Compacting this chat’s context… New messages will send when it finishes.'
}

export function resetChatCompactionForTests() {
  compactingChats.clear()
  emit()
}
