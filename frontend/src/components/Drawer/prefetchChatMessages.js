// Keep drawer intent warming on the same canonical query contract as ChatView.
import { chatQueries } from '../../hooks/queries.js'

export function prefetchChatMessages(queryClient, chatId) {
  return queryClient.prefetchQuery({
    queryKey: chatQueries.messages.key(chatId),
    queryFn: ({ signal }) => chatQueries.messages.fetch(chatId, { signal }),
    staleTime: 30_000,
  })
}
