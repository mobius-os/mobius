/* Query ownership for one chat's recorded edits. */

import { apiFetch } from '../../api/client.js'
import { loadChatDiffEntries } from './chatDiffs.js'

export function chatEditDiffsQueryKey(chatId) {
  return ['chat-edit-diffs', String(chatId || '')]
}

export function chatEditDiffsQueryOptions(chatId) {
  return {
    queryKey: chatEditDiffsQueryKey(chatId),
    // This owner route scans the complete persisted transcript. The mounted
    // message window is only a live supplement in useChatChanges.
    queryFn: ({ signal } = {}) => loadChatDiffEntries(
      chatId,
      { request: apiFetch, signal },
    ),
    staleTime: 15000,
    retry: false,
  }
}

export function invalidateChatChangesQueries(queryClient, chatId) {
  if (!queryClient || !chatId) return Promise.resolve()
  return queryClient.invalidateQueries({
    queryKey: chatEditDiffsQueryKey(chatId),
    exact: true,
  })
}
