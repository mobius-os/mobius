/* Cached Möbius account reads (identity, model access, Railway inventory), the profile edits every surface shares, and the cheap summary Settings shows on its Accounts row. */
import { useEffect } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { isSharedBrowserAuth } from '../api/client.js'
import { authQueries, modelQueries } from './queries.js'
import useAuthedImageUrl, { forgetAuthedImage } from './useAuthedImageUrl.js'
import { accountState, mobiusAccessRequest } from '../lib/mobiusAccount.js'

// Kept in memory only: ['identity', …] is not in queryClient.js's persisted
// allowlist, so the private email and deployments never reach IndexedDB.
const rootKey = ['identity']
const identityKey = [...rootKey, 'account']
const agentKey = [...rootKey, 'agent']
const railwayKey = [...rootKey, 'railway']
// Persisted (queryClient.js): only what the Accounts row paints. Its own root,
// so account invalidations never try to fetch it.
const summaryKey = ['identity-summary']

const fetchIdentity = ({ signal } = {}) => mobiusAccessRequest('/identity', { signal })
const fetchAgent = ({ signal } = {}) => mobiusAccessRequest('/identity/agent', { signal })
const fetchRailway = ({ signal } = {}) => mobiusAccessRequest('/identity/railway?dashboard=1', { signal })

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

function refreshProviders(queryClient) {
  void authQueries.provider.statuses.invalidate(queryClient)
  void modelQueries.registry.invalidate(queryClient)
}

const setIdentity = (queryClient, identity) => queryClient.setQueryData(identityKey, identity)

export const mobiusAccountQueries = {
  key: rootKey,
  identity: { key: identityKey, useQuery: useIdentityQuery },
  agent: { key: agentKey, useQuery: useAgentQuery },
  railway: { key: railwayKey, useQuery: useRailwayQuery },
  /**
   * Show the identity a sign-in just returned and re-read the rest of the
   * account, plus the provider status and model list the account changes, so
   * every sign-in surface (Settings, the first-run guide) leaves the picker
   * current.
   */
  signedIn: (queryClient, identity) => {
    setIdentity(queryClient, identity)
    refreshProviders(queryClient)
    return queryClient.invalidateQueries({ queryKey: rootKey, predicate: query => query.queryKey[1] !== 'account' })
  },
  /** Forget the signed-out account's deployments and agent, then re-read the rest. */
  signedOut: queryClient => {
    queryClient.removeQueries({ queryKey: railwayKey })
    queryClient.removeQueries({ queryKey: agentKey })
    refreshProviders(queryClient)
    return queryClient.invalidateQueries({ queryKey: rootKey })
  },
  /** Re-read everything account-shaped, e.g. when the account page is shown again. */
  invalidate: queryClient => queryClient.invalidateQueries({ queryKey: rootKey }),
  /**
   * Claim or change the handle. Both edits (the first-run guide and Settings)
   * cache the account's answer, and reject with a user-facing message.
   */
  saveHandle: async (queryClient, handle) => {
    setIdentity(queryClient, await mobiusAccessRequest('/identity/profile', { method: 'PATCH', body: JSON.stringify({ handle }) }))
  },
  /** Upload a new photo (already checked with avatarProblem). */
  uploadAvatar: async (queryClient, file) => {
    const body = new FormData()
    body.append('avatar', file)
    const next = await mobiusAccessRequest('/identity/avatar', { method: 'POST', body })
    // The account may keep the same avatar address for new bytes.
    forgetAuthedImage(AVATAR_PATH)
    setIdentity(queryClient, next)
  },
}

/** What Settings keeps on disk about the account; never the email or deployments. */
export function accountSummary(identity) {
  return {
    status: accountState(identity),
    mode: identity.account_mode || null,
    handle: identity.profile?.handle || null,
    avatar: identity.profile?.avatar_url || '',
  }
}

/**
 * The account at a glance for the Accounts row and the Möbius page header:
 * { status, handle, avatarUrl, mode }. `status` is 'loading' | 'signed_out' |
 * 'linked' | 'managed' | 'unavailable' ('unavailable' also covers a failed
 * first read). Shares its cache with the Möbius page; after a reload the
 * last-known summary paints until the live identity read lands.
 */
export function useMobiusAccountSummary() {
  const queryClient = useQueryClient()
  const identity = useIdentityQuery()
  const saved = useQuery({ queryKey: summaryKey, enabled: false, staleTime: Infinity })
  useEffect(() => {
    if (identity.data) queryClient.setQueryData(summaryKey, accountSummary(identity.data))
  }, [queryClient, identity.data])
  const summary = identity.data ? accountSummary(identity.data) : ownerOnly(true) ? saved.data : null
  const avatar = useAuthedImageUrl(AVATAR_PATH, summary?.avatar || '')
  // The saved summary only paints until the live read settles: a failed read
  // reports the account unavailable rather than its last-known state.
  const status = identity.isError && !identity.data
    ? 'unavailable'
    : summary?.status || (identity.isPending ? 'loading' : 'unavailable')
  return {
    status,
    mode: summary?.mode || null,
    handle: summary?.handle || null,
    avatarUrl: avatar.url,
    // True while a photo is expected but not yet shown, so the row can hold a
    // quiet placeholder instead of swapping the Möbius mark for the photo.
    avatarPending: status === 'loading' || avatar.status === 'loading',
  }
}
