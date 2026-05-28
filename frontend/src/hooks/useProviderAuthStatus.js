import { useEffect } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { authQueries } from './queries.js'

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
 *      a tab waking from background gets a fresh check exactly once
 *      per wake — covering the "phone slept overnight, refresh token
 *      expired in the meantime" case without polling.
 *
 *   2. **Single granularity exposed.** Returns `connected | disconnected`
 *      per provider — not `expired` vs `disconnected`. The backend's
 *      `/providers/status` endpoint today returns `{authenticated: bool}`
 *      and doesn't distinguish "never connected" from "expired refresh
 *      token" without correlating with chat 401 history. We picked the
 *      simpler shape so this hook needs no backend change; the
 *      "refresh-token expired" failure mode still surfaces — it just
 *      shows up as `disconnected` until the user reconnects via
 *      Settings.
 *
 * Returns:
 *   {
 *     statuses: { [providerId]: 'connected' | 'disconnected' },
 *     anyDisconnected: boolean,
 *     isLoading: boolean,
 *     isError: boolean,
 *   }
 *
 * Callers can read `statuses.claude` etc directly, or use
 * `anyDisconnected` to drive a single "something needs attention"
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
  // exactly the moments when a refresh token may have silently expired.
  useEffect(() => {
    function onVisibility() {
      if (document.visibilityState === 'visible') {
        authQueries.provider.statuses.invalidate(queryClient)
      }
    }
    document.addEventListener('visibilitychange', onVisibility)
    return () => document.removeEventListener('visibilitychange', onVisibility)
  }, [queryClient])

  // Normalize the backend shape into the friendlier id→status map.
  // The endpoint returns `{claude: {authenticated: bool, ...}, codex: {...}}`;
  // we collapse to `{claude: 'connected'|'disconnected', codex: ...}`.
  const raw = query.data || {}
  const statuses = {}
  let anyDisconnected = false
  for (const [pid, info] of Object.entries(raw)) {
    const connected = !!(info && info.authenticated)
    statuses[pid] = connected ? 'connected' : 'disconnected'
    if (!connected) anyDisconnected = true
  }

  return {
    statuses,
    anyDisconnected,
    isLoading: query.isLoading,
    isError: query.isError,
  }
}
