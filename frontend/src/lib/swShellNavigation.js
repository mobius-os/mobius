/**
 * Serve the current shell while online and one coherent precached generation
 * while offline. Navigation HTML is never written to a second runtime cache.
 */
export async function serveShellNavigation({
  request,
  fetchFresh,
  matchPrecache,
  errorResponse,
}) {
  try {
    const response = await fetchFresh(request)
    if (response?.ok) return response
  } catch { /* offline — use the coherent precached generation below */ }
  return (
    (await matchPrecache('/index.html'))
    || (await matchPrecache('/offline.html'))
    || errorResponse()
  )
}
