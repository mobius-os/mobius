import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { authQueries } from './queries.js'
import {
  providerAvailabilityNeedsAttention,
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
 *     phase: 'loading' | 'ready' | 'error',
 *     configuredProviders: Set<string>,
 *     needsAttention: boolean,
 *   }
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

  return {
    ...availability,
    needsAttention: providerAvailabilityNeedsAttention(availability),
    refetch: query.refetch,
  }
}
