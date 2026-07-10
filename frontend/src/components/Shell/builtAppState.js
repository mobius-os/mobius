export function chatKey(chatId) {
  if (chatId === null || chatId === undefined || chatId === '') return null
  return String(chatId)
}

export function builtAppForChat(builtAppsByChatId, chatId) {
  const key = chatKey(chatId)
  if (!key) return null
  return builtAppsByChatId?.[key] || null
}

export function withBuiltAppForChat(builtAppsByChatId, chatId, builtApp) {
  const key = chatKey(chatId)
  if (!key || !builtApp?.id) return builtAppsByChatId || {}
  return {
    ...(builtAppsByChatId || {}),
    [key]: builtApp,
  }
}

export function withoutBuiltAppForChat(builtAppsByChatId, chatId) {
  const key = chatKey(chatId)
  if (!key || !builtAppsByChatId?.[key]) return builtAppsByChatId || {}
  const { [key]: _removed, ...rest } = builtAppsByChatId
  return rest
}
