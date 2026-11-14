// `@tanstack/react-query` for components a test drives with the hook shim
// (vite-env-loader.mjs): the same QueryObserver the real useQuery uses,
// subscribed through the shim's hooks. A test installs its QueryClient with
// setShimQueryClient before rendering.
import { QueryObserver } from '@tanstack/react-query'
import { useCallback, useMemo, useSyncExternalStore } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

let client = null

export function setShimQueryClient(next) { client = next }

export function useQueryClient() {
  if (!client) throw new Error('setShimQueryClient must run before rendering')
  return client
}

export function useQuery(options) {
  const queryClient = useQueryClient()
  const observer = useMemo(() => new QueryObserver(queryClient, options), [queryClient])
  observer.setOptions(options)
  const subscribe = useCallback(onChange => observer.subscribe(onChange), [observer])
  const current = useCallback(() => observer.getCurrentResult(), [observer])
  return useSyncExternalStore(subscribe, current)
}
