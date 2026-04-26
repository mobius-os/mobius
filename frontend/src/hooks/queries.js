/**
 * Centralized TanStack Query keys.
 *
 * Why keys live here instead of inline in each hook: components AND
 * mutations both need to reference the same key (consumer reads via
 * useQuery, mutator invalidates). Keeping the keys in one file
 * eliminates the "I changed the key in the hook but forgot the
 * invalidation site" class of bug. Use these from any consumer.
 *
 * The hooks themselves currently live in their owning feature files
 * (`useTheme.js` for theme; ChatView.jsx reads the cache directly
 * for messages). When more consumers are added (drawer chat list,
 * app list), they get a hook here to centralize the queryFn.
 */

export const themeQueryKey = ['theme']
export const chatsQueryKey = ['chats']
export const chatMessagesQueryKey = (chatId) => ['chat-messages', chatId]
export const appsQueryKey = ['apps']
