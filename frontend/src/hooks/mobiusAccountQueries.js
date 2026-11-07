/* Cached Möbius account reads (identity, model access, picker visibility, Railway inventory) and the cheap summary Settings shows on its Accounts row. */
import { useQuery } from '@tanstack/react-query'
import { isSharedBrowserAuth } from '../api/client.js'
import useAuthedImageUrl from './useAuthedImageUrl.js'
import { accountState, mobiusAccessRequest } from '../lib/mobiusAccount.js'

// Kept in memory only: ['identity', …] is not in queryClient.js's persisted
// allowlist, so the private email and avatar never reach IndexedDB.
const rootKey = ['identity']
const identityKey = [...rootKey, 'account']
const agentKey = [...rootKey, 'agent']
const visibilityKey = [...rootKey, 'models-visible']
const railwayKey = [...rootKey, 'railway']

const fetchIdentity = ({ signal } = {}) => mobiusAccessRequest('/identity', { signal })
const fetchAgent = ({ signal } = {}) => mobiusAccessRequest('/identity/agent', { signal })
const fetchRailway = ({ signal } = {}) => mobiusAccessRequest('/identity/railway?dashboard=1', { signal })
async function fetchVisibility({ signal } = {}) {
  const body = await mobiusAccessRequest('/auth/providers/mobius/enabled', { signal })
  if (typeof body?.enabled !== 'boolean') throw new Error('Möbius returned an invalid model setting.')
  return body.enabled
}

/** The owner's account photo, served by this Möbius behind auth (see useAuthedImageUrl.js). */
export const AVATAR_PATH = '/identity/avatar'

// The account is the owner's own: its handle, private email, deployments and
// credit are never read for a shared-browser guest.
const ownerOnly = enabled => enabled && !isSharedBrowserAuth()

function useIdentityQuery({ enabled = true } = {}) {
  return useQuery({
    queryKey: identityKey,
    queryFn: fetchIdentity,
    enabled: ownerOnly(enabled),
    staleTime: 60_000,
    // Signing in, a handle change on another device or an outage ending should
    // show up when the owner comes back, as Möbius · You did.
    refetchOnWindowFocus: true,
    retry: 1,
  })
}

function useAgentQuery({ enabled = true } = {}) {
  return useQuery({ queryKey: agentKey, queryFn: fetchAgent, enabled: ownerOnly(enabled), staleTime: 30_000, refetchOnWindowFocus: true, retry: 0 })
}

function useVisibilityQuery() {
  return useQuery({ queryKey: visibilityKey, queryFn: fetchVisibility, enabled: ownerOnly(true), staleTime: 60_000, retry: 0 })
}

function useRailwayQuery({ enabled = true, refetchInterval = false } = {}) {
  return useQuery({
    queryKey: railwayKey,
    queryFn: fetchRailway,
    enabled: ownerOnly(enabled),
    staleTime: 15_000,
    retry: 0,
    refetchInterval,
  })
}

export const mobiusAccountQueries = {
  key: rootKey,
  identity: { key: identityKey, useQuery: useIdentityQuery },
  agent: { key: agentKey, useQuery: useAgentQuery },
  visibility: { key: visibilityKey, useQuery: useVisibilityQuery },
  railway: { key: railwayKey, useQuery: useRailwayQuery },
  /** Show the identity a sign-in just returned and re-read the rest of the account. */
  signedIn: (queryClient, identity) => {
    queryClient.setQueryData(identityKey, identity)
    return queryClient.invalidateQueries({ queryKey: rootKey, predicate: query => query.queryKey[1] !== 'account' })
  },
  /** Re-read everything account-shaped, e.g. after signing in or out. */
  invalidate: queryClient => queryClient.invalidateQueries({ queryKey: rootKey }),
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
  const avatar = useAuthedImageUrl(AVATAR_PATH, data?.profile?.avatar_url || '')
  const status = data ? accountState(data) : identity.isPending ? 'loading' : 'unavailable'
  return {
    status,
    mode: data?.account_mode || null,
    handle: data?.profile?.handle || null,
    avatarUrl: avatar.url,
    // True while a photo is expected but not yet shown, so the row can hold a
    // quiet placeholder instead of swapping the Möbius mark for the photo.
    avatarPending: status === 'loading' || avatar.status === 'loading',
  }
}
