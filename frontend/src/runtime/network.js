const READ_TIMEOUT_MS = 2500

export function fetchBounded(url, init) {
  const ctrl = typeof AbortController !== 'undefined' ? new AbortController() : null
  const opts = ctrl ? { ...init, signal: ctrl.signal } : init
  let timer
  if (ctrl) timer = setTimeout(() => ctrl.abort(), READ_TIMEOUT_MS)
  return fetch(url, opts).finally(() => { if (timer) clearTimeout(timer) })
}

// App-token fetch with one bounded refresh retry. Hosts implement
// getToken({forceRefresh:true}) differently (the shell asks AppCanvas; the
// standalone host remints with its owner token), while every runtime subsystem
// gets the same auth lifecycle and never invents its own stale-token cache.
export async function fetchWithAppToken(getToken, url, init = {}, fetcher = fetch) {
  const run = async (token) => {
    if (!token) throw new Error('mobius: app token unavailable')
    return fetcher(url, {
      ...init,
      headers: { ...(init.headers || {}), Authorization: `Bearer ${token}` },
    })
  }
  const token = await getToken()
  let response = await run(token)
  if (response.status !== 401) return response
  const refreshed = await getToken({ forceRefresh: true })
  if (!refreshed || refreshed === token) return response
  response = await run(refreshed)
  return response
}
