/* Cached Möbius account reads (identity, model access, Railway inventory), the profile edits every surface shares, and the cheap summary Settings shows on its Accounts row. */
import { useQuery } from '@tanstack/react-query'
import { isSharedBrowserAuth } from '../api/client.js'
import { authQueries, modelQueries } from './queries.js'
import useAuthedImageUrl, { forgetAuthedImage } from './useAuthedImageUrl.js'
import { accountState, mobiusAccessRequest } from '../lib/mobiusAccount.js'

// Kept in memory only: ['identity', …] is not in queryClient.js's persisted
// allowlist, so the private email, deployments and avatar address never reach
// IndexedDB.
const rootKey = ['identity']
const identityKey = [...rootKey, 'account']
const agentKey = [...rootKey, 'agent']
const railwayKey = [...rootKey, 'railway']
// Persisted (queryClient.js): only what the Accounts row paints. Its own root,
// so account invalidations never try to fetch it.
const summaryKey = ['identity-summary']

const fetchIdentity = async ({ signal, client }) => rememberIdentity(client, await mobiusAccessRequest('/identity', { signal }))
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

// Every identity the cache takes in, read or returned by an edit, also
// refreshes the saved summary, so a change made from any surface (the
// first-run guide too) reaches disk whether or not Settings is open.
function rememberIdentity(queryClient, identity) {
  if (identity) queryClient.setQueryData(summaryKey, accountSummary(identity))
  return identity
}

const setIdentity = (queryClient, identity) => queryClient.setQueryData(identityKey, rememberIdentity(queryClient, identity))

// A sign-in's or an edit's answer is newer than any account read still under
// way: cancel those first (as saveModelPrefsChanges does), so an older answer
// cannot land after it and overwrite the account and its saved summary.
async function acceptIdentity(queryClient, identity) {
  await queryClient.cancelQueries({ queryKey: identityKey })
  setIdentity(queryClient, identity)
}

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
  signedIn: async (queryClient, identity) => {
    await acceptIdentity(queryClient, identity)
    refreshProviders(queryClient)
    return queryClient.invalidateQueries({ queryKey: rootKey, predicate: query => query.queryKey[1] !== 'account' })
  },
  /**
   * Forget the signed-out account's deployments and agent, then re-read the
   * rest. The saved summary turns signed-out at once, past any account read
   * still under way (as acceptIdentity does), so a reload whose read fails
   * never paints the old handle.
   */
  signedOut: async queryClient => {
    await queryClient.cancelQueries({ queryKey: identityKey })
    queryClient.setQueryData(summaryKey, accountSummary({ account_mode: 'signed_out' }))
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
    await acceptIdentity(queryClient, await mobiusAccessRequest('/identity/profile', { method: 'PATCH', body: JSON.stringify({ handle }) }))
  },
  /** Upload a new photo (already checked with avatarProblem). */
  uploadAvatar: async (queryClient, file) => {
    const body = new FormData()
    body.append('avatar', file)
    const next = await mobiusAccessRequest('/identity/avatar', { method: 'POST', body })
    // The account may keep the same avatar address for new bytes.
    forgetAuthedImage(AVATAR_PATH)
    await acceptIdentity(queryClient, next)
  },
}

/**
 * What names the account's photo in the image cache: a short fingerprint of
 * its avatar address ('' without one), so every surface showing the photo
 * shares one read and the saved summary never holds the third-party address.
 * A new photo gets a new address, or forgetAuthedImage drops the old bytes.
 */
export function avatarKey(identity) {
  const address = identity?.profile?.avatar_url
  if (typeof address !== 'string' || !address) return ''
  // FNV-1a: a stable, synchronous 32-bit fingerprint is enough to tell photos apart.
  let hash = 0x811c9dc5
  for (let index = 0; index < address.length; index += 1) {
    hash = Math.imul(hash ^ address.charCodeAt(index), 0x01000193)
  }
  return `avatar-${(hash >>> 0).toString(36)}`
}

/** What Settings keeps on disk about the account; never the email, deployments or avatar address. */
export function accountSummary(identity) {
  return {
    status: accountState(identity),
    mode: identity.account_mode || null,
    handle: identity.profile?.handle || null,
    avatar: avatarKey(identity),
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
  const identity = useIdentityQuery()
  const saved = useQuery({ queryKey: summaryKey, enabled: false, staleTime: Infinity })
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
