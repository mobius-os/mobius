/* One query identity and freshness policy owns exact-chat activity reads. */
export function chatActivityQueryKey(chatId) {
  return ['chat-activity', String(chatId)]
}

// Activity changes and system reconnects invalidate this cache explicitly.
// Keeping it fresh until one of those signals arrives avoids refetching a
// complete timeline just because the chat remounted during a server restart.
export const CHAT_ACTIVITY_STALE_TIME = Infinity

// No owner-facing error row exists for this optional read, so the query itself
// must heal. A restart or network gap fails the request without an HTTP status
// (or with a gateway 5xx); retry those with backoff (1+2+4+8s spans a normal
// restart). A 4xx is a real answer and is not retried. Reconnect and activity
// invalidation still refetch anything that outlasts this budget.
const CHAT_ACTIVITY_TRANSIENT_RETRIES = 4

export function retryChatActivity(failureCount, error) {
  if (error?.name === 'AbortError') return false
  const status = error?.status
  if (typeof status === 'number' && status < 500 && status !== 408 && status !== 429) return false
  return failureCount < CHAT_ACTIVITY_TRANSIENT_RETRIES
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
