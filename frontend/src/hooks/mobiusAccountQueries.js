/* Cached Möbius account reads (identity, avatar, model access, picker visibility, Railway inventory) and the cheap summary Settings shows on its Accounts row. */
import { useQuery } from '@tanstack/react-query'
import { apiFetch } from '../api/client.js'
import { objectUrlFor } from './useAuthedImageUrl.js'
import { accountState, mobiusAccessRequest } from '../lib/mobiusAccount.js'

// Kept in memory only: ['identity', …] is not in queryClient.js's persisted
// allowlist, so the private email and avatar never reach IndexedDB.
const rootKey = ['identity']
const identityKey = [...rootKey, 'account']
const avatarKey = avatarUrl => [...rootKey, 'avatar', avatarUrl || '']
const agentKey = [...rootKey, 'agent']
const visibilityKey = [...rootKey, 'models-visible']
const railwayKey = [...rootKey, 'railway']

const fetchIdentity = ({ signal } = {}) => mobiusAccessRequest('/api/identity', { signal })
const fetchAgent = ({ signal } = {}) => mobiusAccessRequest('/api/identity/agent', { signal })
const fetchRailway = ({ signal } = {}) => mobiusAccessRequest('/api/identity/railway?dashboard=1', { signal })
async function fetchVisibility({ signal } = {}) {
  const body = await mobiusAccessRequest('/api/auth/providers/mobius/enabled', { signal })
  if (typeof body?.enabled !== 'boolean') throw new Error('Möbius returned an invalid model setting.')
  return body.enabled
}

// The avatar is served by the owner's backend behind auth, so it is read as a
// blob. Each consumer turns the cached blob into its own object URL.
async function fetchAvatar({ signal } = {}) {
  const response = await apiFetch('/identity/avatar', { signal, timeoutMs: 15_000 })
  if (!response.ok) return null
  const blob = await response.blob()
  return blob.type.startsWith('image/') ? blob : null
}

function useIdentityQuery({ enabled = true } = {}) {
  return useQuery({
    queryKey: identityKey,
    queryFn: fetchIdentity,
    enabled,
    staleTime: 60_000,
    // Signing in, a handle change on another device or an outage ending should
    // show up when the owner comes back, as Möbius · You did.
    refetchOnWindowFocus: true,
    retry: 1,
  })
}

function useAvatarQuery(avatarUrl) {
  return useQuery({
    queryKey: avatarKey(avatarUrl),
    queryFn: fetchAvatar,
    enabled: Boolean(avatarUrl),
    staleTime: Infinity,
    // Kept for the page's life between Settings visits, so the photo never
    // re-downloads (and blinks) just because Settings was closed for a while.
    gcTime: 60 * 60_000,
    retry: 0,
  })
}

function useAgentQuery({ enabled = true } = {}) {
  return useQuery({ queryKey: agentKey, queryFn: fetchAgent, enabled, staleTime: 30_000, refetchOnWindowFocus: true, retry: 0 })
}

function useVisibilityQuery() {
  return useQuery({ queryKey: visibilityKey, queryFn: fetchVisibility, staleTime: 60_000, retry: 0 })
}

function useRailwayQuery({ enabled = true, refetchInterval = false } = {}) {
  return useQuery({
    queryKey: railwayKey,
    queryFn: fetchRailway,
    enabled,
    staleTime: 15_000,
    retry: 0,
    refetchInterval,
  })
}

export const mobiusAccountQueries = {
  key: rootKey,
  identity: { key: identityKey, fetch: fetchIdentity, useQuery: useIdentityQuery },
  avatar: { keyFor: avatarKey, fetch: fetchAvatar, useQuery: useAvatarQuery },
  agent: { key: agentKey, fetch: fetchAgent, useQuery: useAgentQuery },
  visibility: { key: visibilityKey, fetch: fetchVisibility, useQuery: useVisibilityQuery },
  railway: { key: railwayKey, fetch: fetchRailway, useQuery: useRailwayQuery },
  /** Re-read everything account-shaped, e.g. after signing in or out. */
  invalidate: queryClient => queryClient.invalidateQueries({ queryKey: rootKey }),
}

/** The stable object URL for a cached avatar blob (see useAuthedImageUrl.js), available on first render. */
export function useObjectUrl(blob) {
  return objectUrlFor(blob)
}

/**
 * The account at a glance for the Accounts row and the Möbius page header:
 * { status, handle, avatarUrl, mode }. `status` is 'loading' | 'signed_out' |
 * 'linked' | 'managed' | 'unavailable' ('unavailable' also covers a failed
 * first read). Shares its cache with the Möbius page.
 */
export function useMobiusAccountSummary() {
  const identity = useIdentityQuery()
  const data = identity.data
  const avatar = useAvatarQuery(data?.profile?.avatar_url)
  const avatarUrl = useObjectUrl(avatar.data || null)
  const status = data ? accountState(data) : identity.isPending ? 'loading' : 'unavailable'
  return {
    status,
    mode: data?.account_mode || null,
    handle: data?.profile?.handle || null,
    avatarUrl,
    // True while a photo is expected but not yet shown, so the row can hold a
    // quiet placeholder instead of swapping the Möbius mark for the photo.
    avatarPending: status === 'loading' || (Boolean(data?.profile?.avatar_url) && !avatarUrl && avatar.isPending),
  }
}
