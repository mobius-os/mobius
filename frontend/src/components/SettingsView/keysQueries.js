/* The owner's keys and requests (GET /api/keys) and installed app names, shared by Settings › Keys and the key request cards in chats and over apps. */
import { useQuery } from '@tanstack/react-query'
import { api, isSharedBrowserAuth, jsonOrThrow } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'
import { distinctAppNames } from '../../lib/keysSettingsText.js'

export const KEYS_QUERY = ['settings', 'keys']

/**
 * Keys and requests, owner only: a shared-browser guest never asks (the
 * route refuses it), so its cards stay read-only. Extra options tune when an
 * observer refetches, such as on window focus for a card over an app.
 */
export function useKeys({ enabled = true, ...options } = {}) {
  return useQuery({
    queryKey: KEYS_QUERY,
    queryFn: async ({ signal }) => jsonOrThrow(await api.keys.list({ signal }), 'Keys'),
    staleTime: 0,
    ...options,
    enabled: enabled && !isSharedBrowserAuth(),
  })
}

/** Installed apps by numeric id: names and slugs can be reused. */
export function useAppNames() {
  const appsQuery = appQueries.list.useQuery()
  const apps = distinctAppNames((appsQuery.data || [])
    .filter(app => Number.isInteger(app.id))
    // The installation, because a removed app's id can be reused.
    .map(app => ({ id: app.id, name: app.name || app.slug || `App ${app.id}`, installation: app.storage_generation })))
    .sort((a, b) => a.name.localeCompare(b.name))
  return { apps, names: Object.fromEntries(apps.map(app => [app.id, app.name])) }
}
