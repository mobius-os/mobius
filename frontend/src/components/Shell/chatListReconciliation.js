// Own the cancellable, live-only drawer read used at committed state boundaries.
import { chatQueries } from '../../hooks/queries.js'

export async function fetchFreshChatList(queryClient, {
  signal,
  timeoutMs,
  reconcile = rows => rows,
} = {}) {
  signal?.throwIfAborted()
  // Do not join a pre-transition request and let its old snapshot overwrite a
  // committed question answer or run event. Cancellation is part of the read.
  await queryClient.cancelQueries({ queryKey: chatQueries.keys.all })
  signal?.throwIfAborted()
  const data = await queryClient.fetchQuery({
    queryKey: chatQueries.keys.all,
    queryFn: async ({ signal: querySignal }) => {
      const requestSignal = signal ? AbortSignal.any([signal, querySignal]) : querySignal
      const rows = await chatQueries.list.fetch({
        timeoutMs, signal: requestSignal, cache: 'no-store',
      })
      // A replaced reconnect may finish decoding late. Its result must never
      // enter the shared query cache, even when the transport ignored abort.
      requestSignal.throwIfAborted()
      return reconcile(rows)
    },
    staleTime: 0,
    // The system connection owns recovery/backoff, not an overlapping query retry.
    retry: false,
  })
  return data || []
}
