import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { authQueries } from './queries.js'
import {
  PROVIDER_AVAILABILITY_PHASE,
  resolveProviderAvailability,
} from '../lib/providerAvailability.js'

/**
 * Passive provider auth status — reads /api/auth/providers/status
 * via the existing TanStack Query plumbing (authQueries.provider.statuses).
 *
 * Why this hook exists separately from the raw query:
 *
 *   1. **No polling.** Provider status almost never changes within a
 *      session (only re-auth flips it), so a polled refetch wastes
 *      bytes. Instead we let the query's default behavior cache the
 *      result, and we invalidate on `visibilitychange → visible` so
 *      a tab waking from background gets a fresh local credential check
 *      exactly once per wake, without polling.
 *
 *   2. **Honest granularity.** The endpoint reports whether local provider
 *      credentials are configured; it does not make a network call to prove
 *      that a remote token is still accepted.
 *
 * Returns:
 *   {
 *     statuses: { [providerId]: 'connected' | 'disconnected' },
 *     anyDisconnected: boolean,
 *     phase: 'loading' | 'ready' | 'error',
 *     connectedProviders: Set<string>,
 *     needsAttention: boolean,
 *     isError: boolean,
 *   }
 *
 * Callers can read `statuses.claude` etc directly, or use
 * `needsAttention` to drive a single "something needs attention"
 * indicator (the drawer Settings warning dot).
 */
export default function useProviderAuthStatus() {
  const queryClient = useQueryClient()
  // staleTime: 5 min — the data rarely changes within a session, and
  // the visibilitychange invalidation below is what actually refreshes
  // it on wake. Setting staleTime too low would cause spurious refetches
  // on every component remount.
  const query = authQueries.provider.statuses.useQuery()

  // Invalidate on visibility=visible so a tab waking from background
  // (phone unlocked, app foregrounded) triggers a single fresh check.
  // This is the lever instead of polling — wake events are sparse and
  // exactly the moments when credentials may have changed in another tab.
  useEffect(() => {
    function onVisibility() {
      if (document.visibilityState === 'visible') {
        authQueries.provider.statuses.invalidate(queryClient)
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [queryClient])

  const availability = resolveProviderAvailability(query)

  // Normalize the backend shape into the friendlier id→status map.
  // The endpoint returns `{claude: {configured: bool, ...}, codex: {...}}`;
  // we collapse to `{claude: 'connected'|'disconnected', codex: ...}`.
  const raw = query.data || {}
  const statuses = {}
  let anyDisconnected = false
  for (const pid of Object.keys(raw)) {
    const connected = availability.connectedProviders.has(pid)
    statuses[pid] = connected ? 'connected' : 'disconnected'
    if (!connected) anyDisconnected = true
  }

  return {
    ...availability,
    statuses,
    anyDisconnected,
    needsAttention:
      availability.phase === PROVIDER_AVAILABILITY_PHASE.ERROR
      || anyDisconnected,
    isLoading: query.isLoading,
    isError: query.isError,
    refetch: query.refetch,
  }
}
