/* One query identity and freshness policy owns exact-chat activity reads. */
export function chatActivityQueryKey(chatId) {
  return ['chat-activity', String(chatId)]
}

// Activity changes and system reconnects invalidate this cache explicitly.
// Keeping it fresh until one of those signals arrives avoids refetching a
// complete timeline just because the chat remounted during a server restart.
export const CHAT_ACTIVITY_STALE_TIME = Infinity

export function invalidateChatActivityForSystemEvent(queryClient, event) {
  if (event?.type === 'chat_activity_changed' && event.chatId) {
    return queryClient.invalidateQueries({
      queryKey: chatActivityQueryKey(event.chatId),
    })
  }
  if (event?.type === 'agent_coordination_message') {
    const affected = new Set([
      String(event.senderChatId || ''),
      ...(event.recipientChatIds || []).map(String),
    ])
    return queryClient.invalidateQueries({ predicate: query => (
      query.queryKey[0] === 'chat-activity'
      && (event.broadcast || affected.has(String(query.queryKey[1])))
    ) })
  }
  return Promise.resolve()
}

export function invalidateAllChatActivity(queryClient) {
  return queryClient.invalidateQueries({ queryKey: ['chat-activity'] })
}
