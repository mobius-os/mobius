export const APP_FRAME_EDGE_PROBE_PATH = '/api/apps/0/frame'

/** Read the policy that the public edge actually applies to app frames.
 *
 * HEAD is deliberately outside the service worker's GET-only app-code route,
 * and app id 0 can never name an installed app. The response may be 404; only
 * its edge-selected headers matter. A unique query plus `no-store` prevents a
 * browser/CDN cache from turning an old policy into fresh evidence.
 */
export async function probeAppFrameEdgePolicy(
  fetchImpl = globalThis.fetch,
  now = Date.now,
) {
  const url = `${APP_FRAME_EDGE_PROBE_PATH}?mobius_edge_preflight=${now()}`
  const response = await fetchImpl(url, {
    method: 'HEAD',
    cache: 'no-store',
    credentials: 'same-origin',
  })
  return {
    path: APP_FRAME_EDGE_PROBE_PATH,
    content_security_policy: response.headers.get('content-security-policy'),
  }
}

export const EDGE_POLICY_UNVERIFIED_MESSAGE =
  'Couldn’t verify the public app-frame security policy, so nothing was changed. '
  + 'Check the host proxy, then try again.'

/** Apply a reviewed update only with a freshly measured edge policy attached.
 *
 * The ordering is the contract: the CSP the public edge serves decides whether
 * mini-apps can still execute after the rebuild, so an unreadable edge must
 * stop the update BEFORE any source changes rather than after. Binding the
 * probe and the apply call into one unit keeps a future caller from issuing an
 * apply that skips the probe, and makes the precondition testable without
 * rendering Settings.
 */
export async function applyWithFreshEdgePolicy(plan, { probe, apply }) {
  let edgePreflight
  try {
    edgePreflight = await probe()
  } catch {
    return { error: EDGE_POLICY_UNVERIFIED_MESSAGE }
  }
  return { response: await apply({ ...plan, edge_preflight: edgePreflight }) }
}
