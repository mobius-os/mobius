/* The instance's GitHub account for Settings: read status, run one device-code sign-in, disconnect. */
import { api } from '../api/client.js'

// The platform stores one GitHub credential for the whole instance; Settings
// is the only UI that changes it, and apps only read /api/github/status.
//
// The server owns the sign-in attempt: it paces GitHub (retry_after), expires
// the code, and reports terminal outcomes. The browser therefore just polls
// one identified attempt sequentially until the server says it is finished.

const REQUEST_TIMEOUT_MS = 60_000
const MAX_CONSECUTIVE_ERRORS = 3

async function detail(response, fallback) {
  const body = await response.json().catch(() => ({}))
  return typeof body?.detail === 'string' && body.detail.trim() ? body.detail.trim() : fallback
}

async function call(request, fallback) {
  const response = await request({ timeoutMs: REQUEST_TIMEOUT_MS })
  if (!response.ok) {
    const error = new Error(await detail(response, fallback))
    error.retryable = response.status >= 500 || response.status === 429
    throw error
  }
  return response.json().catch(() => ({}))
}

export const hasFullPrAccess = scopes => Array.isArray(scopes)
  && scopes.includes('workflow')
  && (scopes.includes('repo') || scopes.includes('public_repo'))

export const hasPrivateRepoAccess = scopes => Array.isArray(scopes) && scopes.includes('repo')

function attemptFrom(body) {
  return body?.attempt_id && body.user_code && body.verification_uri
    ? { attemptId: body.attempt_id, userCode: body.user_code, verificationUri: body.verification_uri }
    : null
}

/** The instance's GitHub status, with a sign-in already waiting on GitHub (another tab, a reload) as `attempt`. Rejects when it cannot be read. */
export async function fetchGithubStatus({ signal } = {}) {
  const s = await call(options => api.github.status({ ...options, signal }), 'Could not check GitHub.')
  return {
    state: s.connected ? 'connected' : 'disconnected',
    login: s.login || '',
    scopes: Array.isArray(s.scopes) ? s.scopes : [],
    signInAvailable: !!s.device_flow_available,
    attempt: attemptFrom(s.active_attempt),
  }
}

/** The status Settings shows when GitHub could not be read. */
export const unknownGithubStatus = error => ({
  state: 'unknown',
  message: error?.message || 'Could not reach the GitHub connection service.',
})

const STATUS_KEY = ['github', 'status']

// The newest status read for each query client, or null once an account
// change has started since. Only the newest read publishes: Settings can
// unmount mid-read (a provider page opens) and start another read on return,
// and the slower one would otherwise land last and replace the newer answer,
// connected or not. An overtaken read answers with the newer read instead, so
// whichever caller acts on a waiting sign-in sees the same status.
const newestReads = new WeakMap()

/**
 * GitHub status in the shared query cache, persisted (queryClient.js) so
 * Settings paints the last confirmed account at once after a reload. The
 * cache holds only that settled account state (connection, login, scopes); a
 * sign-in waiting on GitHub goes to the caller of `refresh` and is never
 * stored. Reads happen when Settings asks, so `options` only subscribes.
 */
export const githubStatus = {
  key: STATUS_KEY,
  options: { queryKey: STATUS_KEY, enabled: false, staleTime: Infinity },
  /**
   * Read the status now and cache its account part. Resolves the full status
   * of the newest read, or null when an account change started meanwhile
   * (its caller reads next and decides, so this caller must not act); rejects
   * when the newest read cannot be read.
   */
  refresh(queryClient) {
    const overtaken = () => newestReads.get(queryClient) !== read
    const read = fetchGithubStatus().then(status => {
      if (overtaken()) return newestReads.get(queryClient)
      const { attempt, ...account } = status
      queryClient.setQueryData(STATUS_KEY, account)
      return { ...account, attempt }
    }, error => {
      if (overtaken()) return newestReads.get(queryClient)
      throw error
    })
    newestReads.set(queryClient, read)
    return read
  },
  /**
   * Run a request that changes the account (disconnect, cancelling a sign-in):
   * every read already under way is obsolete, so none publishes over it.
   * Follow it with `refresh` for the status it left.
   */
  change(queryClient, request) {
    newestReads.set(queryClient, null)
    return request()
  },
}

export async function startGithubSignIn({ privateRepos = false, signal } = {}) {
  const body = await call(options => api.github.connectStart(privateRepos, { ...options, signal }), 'Could not start GitHub sign-in.')
  const attempt = attemptFrom(body)
  if (!attempt) throw new Error('GitHub sign-in started without a code. Please try again.')
  return attempt
}

const OUTCOME_MESSAGES = {
  access_denied: 'GitHub sign-in was denied.',
  expired_token: 'The code expired. Please try again.',
}

// Resolves { status: 'complete' } or { status: 'failed' | 'cancelled', message }.
export async function waitForGithubSignIn(attemptId, { signal, onRetrying = () => {} } = {}) {
  let delayMs = 0
  let errors = 0
  while (!signal?.aborted) {
    await new Promise(resolve => {
      const done = () => {
        clearTimeout(timer)
        signal?.removeEventListener('abort', done)
        resolve()
      }
      const timer = setTimeout(done, delayMs)
      signal?.addEventListener('abort', done, { once: true })
    })
    if (signal?.aborted) break
    let body
    try {
      body = await call(options => api.github.connectPoll(attemptId, { ...options, signal }), 'Could not check GitHub sign-in.')
      errors = 0
      onRetrying(false)
    } catch (error) {
      if (signal?.aborted) break
      errors += 1
      const transient = error.retryable || error.name === 'TimeoutError'
      if (!transient || errors >= MAX_CONSECUTIVE_ERRORS) {
        return { status: 'failed', message: error.message }
      }
      onRetrying(true)
      delayMs = 5_000
      continue
    }
    if (body.status === 'pending') {
      const seconds = Number(body.retry_after ?? body.interval ?? 5)
      delayMs = Math.max(1_000, (Number.isFinite(seconds) ? seconds : 5) * 1000)
      continue
    }
    if (body.status === 'complete') return { status: 'complete' }
    return {
      status: body.status === 'cancelled' ? 'cancelled' : 'failed',
      message: body.message || OUTCOME_MESSAGES[body.reason] || 'GitHub sign-in did not finish. Please try again.',
    }
  }
  return { status: 'cancelled' }
}

export function cancelGithubSignIn(attemptId, { signal } = {}) {
  return call(options => api.github.connectCancel(attemptId, { ...options, signal }), 'Could not cancel GitHub sign-in.')
}

export function disconnectGithub() {
  return call(options => api.github.disconnect(options), 'Could not disconnect GitHub.')
}
