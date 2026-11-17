import { api } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import { failureMessage } from './uninstalledApps.js'

export const ACTIONS = {
  recover: { call: id => api.apps.recover(id), working: 'Recovering…', failed: 'Could not recover this app. Try again.' },
  delete: { call: id => api.apps.deletePermanently(id), working: 'Deleting…', failed: 'Could not delete this app. Try again.' },
}

export const uninstalledActionKey = id => ['uninstalled-app-action', id]

// Runs one action to the end. It is registered as a query-client mutation, which outlives any tile or card, so
// searching the tile away, dismissing its card or leaving the screen cannot lose a request in flight or its failure.
export async function runUninstalledAction(queryClient, id, kind) {
  let response
  try {
    response = await ACTIONS[kind].call(id)
  } catch {
    throw new Error('Could not reach Möbius. Try again.')
  }
  if (!response.ok) throw new Error(await failureMessage(response, ACTIONS[kind].failed))
  await Promise.all([
    appQueries.list.invalidate(queryClient),
    appQueries.uninstalled.invalidate(queryClient),
  ])
}

/** What a tile shows for its app, from the mutation states recorded under uninstalledActionKey. */
export function latestActionState(states) {
  const last = states.at(-1)
  return {
    working: last?.status === 'pending' ? last.kind : null,
    error: last?.status === 'error' ? last.message : '',
  }
}

export const selectActionState = mutation => ({
  status: mutation.state.status,
  kind: mutation.state.variables,
  message: mutation.state.error?.message,
})
