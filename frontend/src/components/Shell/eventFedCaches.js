import {
  appSourceQueries,
  authQueries,
  chatAppArtifactQueries,
  modelQueries,
  themeQueries,
} from '../../hooks/queries.js'
import { invalidateAllChatActivity } from '../ChatView/chatActivityQueries.js'

/** Reconcile event-owned state after a gap in the system stream. */
export function invalidateEventFedCachesAfterReconnect(queryClient) {
  return [
    themeQueries.invalidate(queryClient),
    modelQueries.registry.invalidate(queryClient),
    authQueries.provider.statuses.invalidate(queryClient),
    appSourceQueries.invalidate(queryClient),
    chatAppArtifactQueries.invalidateAll(queryClient),
    invalidateAllChatActivity(queryClient),
    queryClient.invalidateQueries({ queryKey: ['projects', 'files'] }),
    queryClient.invalidateQueries({ queryKey: ['projects', 'git'] }),
  ]
}
