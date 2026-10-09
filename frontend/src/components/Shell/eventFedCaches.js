import {
  authQueries,
  modelQueries,
  appSourceQueries,
  chatAppArtifactQueries,
} from '../../hooks/queries.js'
import { invalidateAllChatActivity } from '../ChatView/chatActivityQueries.js'

// Broadcasts have no replay. Even on the first open, mount-time fetches may
// finish before the stream subscribes, leaving an event-fed cache stale.
export function invalidateEventFedCaches(queryClient) {
  return [
    modelQueries.registry.invalidate(queryClient),
    authQueries.provider.statuses.invalidate(queryClient),
    appSourceQueries.invalidate(queryClient),
    chatAppArtifactQueries.invalidateAll(queryClient),
    invalidateAllChatActivity(queryClient),
    queryClient.invalidateQueries({ queryKey: ['projects', 'files'] }),
    queryClient.invalidateQueries({ queryKey: ['projects', 'git'] }),
  ]
}
