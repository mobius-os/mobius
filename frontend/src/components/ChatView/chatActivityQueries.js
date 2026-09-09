/* One query identity and freshness policy owns exact-chat activity reads. */
export function chatActivityQueryKey(chatId) {
  return ['chat-activity', String(chatId)]
}

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
