import { appQueries } from '../../hooks/queries.js'

// Every uninstall and every recovery reaches the shell as one of these two events, whoever caused it (this
// tab, an agent, another browser or a notification). The recovery list has its own query key, so the
// apps-list refresh does not reach it: this is the one place it is refreshed from.
const EVENTS = new Set(['app_deleted', 'app_recovered'])

export function refreshUninstalledForSystemEvent(queryClient, event) {
  if (!EVENTS.has(event?.type)) return Promise.resolve()
  return appQueries.uninstalled.invalidate(queryClient)
}
