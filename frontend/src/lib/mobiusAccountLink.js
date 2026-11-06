/* Signs a self-hosted owner in to their Möbius account from the top-level shell: popup, PKCE link attempt, validated completion message and a lost-response-safe local completion. */
import { accountLinkCompletion, accountLinkRegistration } from './accountLinkBroker.js'
import { mobiusAccessRequest, retryableAccessError } from './mobiusAccount.js'

// The flow (see the Möbius · You ACCOUNT_LINK_FLOW contract):
//  1. A blank popup opens synchronously inside the click; a blocked popup stops
//     before any link attempt exists.
//  2. POST /api/identity/link/start creates state + PKCE server-side and
//     returns the authorization URL. Its exact origin, the state and a bounded
//     local lifetime are registered here before the popup navigates.
//  3. The account's completion page posts {type:'mobius-account-link',code,state}
//     to window.opener.top at this shell's origin. The shell IS that top
//     window, so the message is validated here with the same broker rules the
//     app frames use (exact origin, state, shape, lifetime) — nothing is
//     forwarded anywhere.
//  4. The popup closes and POST /api/identity/link/complete exchanges the code.
//     A lost response is ambiguous, so GET /api/identity decides before any
//     retry is offered; the retry payload lives only in the caller's memory.
// Every terminal path (blocked, closed by the owner, timeout, refusal, cancel)
// removes the listener, clears timers and closes the popup.

export const ACCOUNT_LINK_PROVIDERS = Object.freeze(['google', 'apple'])
const ACCOUNT_LINK_STATE = /^[A-Za-z0-9_-]{32,512}$/
const ACCOUNT_LINK_ATTEMPT = /^[A-Za-z0-9_-]{16,512}$/
const POPUP_NAME = 'mobius-account-signin'
const POPUP_FEATURES = 'width=520,height=720'

export const ACCOUNT_LINK_MESSAGES = Object.freeze({
  blocked: 'Your browser blocked the sign-in window. Allow pop-ups for this Möbius, then try again.',
  closed: 'The sign-in window was closed. Try again when you are ready.',
  timeout: 'Sign-in took too long. Please try again.',
  navigation: 'The sign-in window could not be opened. Please try again.',
  cancelled: 'Sign-in cancelled.',
  unconfirmed: 'Your account approved the sign-in, but this Möbius could not confirm it. Retry to finish signing in.',
})

/** An owner-visible sign-in failure; `reason` names the terminal path. */
export class AccountLinkError extends Error {
  constructor(reason, message = ACCOUNT_LINK_MESSAGES[reason] || 'Sign-in could not finish.') {
    super(message)
    this.name = 'AccountLinkError'
    this.reason = reason
  }
}

/** Validate POST /api/identity/link/start and pin the authorization origin it names. */
export function parseLinkAttempt(value) {
  if (
    !value || typeof value !== 'object' || Array.isArray(value)
    || typeof value.attempt !== 'string' || !ACCOUNT_LINK_ATTEMPT.test(value.attempt)
    || typeof value.state !== 'string' || !ACCOUNT_LINK_STATE.test(value.state)
    || typeof value.expires_at !== 'string' || !Number.isFinite(Date.parse(value.expires_at))
    || typeof value.authorization_url !== 'string'
  ) throw new AccountLinkError('invalid', 'Möbius returned an invalid sign-in attempt.')
  let authorization
  try {
    authorization = new URL(value.authorization_url)
  } catch {
    authorization = null
  }
  const registration = authorization && accountLinkRegistration({
    type: 'moebius:account-link-register',
    authorizationOrigin: authorization.origin,
    state: value.state,
    expiresAt: value.expires_at,
  })
  if (
    !registration
    || authorization.hash
    || authorization.searchParams.getAll('state').length !== 1
    || authorization.searchParams.get('state') !== value.state
  ) throw new AccountLinkError('invalid', 'Möbius returned an invalid sign-in address.')
  return {
    authorizationUrl: authorization.href,
    authorizationOrigin: authorization.origin,
    attempt: value.attempt,
    state: value.state,
    expiresAt: value.expires_at,
  }
}

/**
 * Navigate `popup` to the attempt and resolve with {code, state} from the
 * validated completion message. Rejects with an AccountLinkError on owner
 * close, timeout, navigation failure or abort. Always cleans up and closes
 * the popup.
 */
export function awaitAccountLinkCompletion({
  popup,
  attempt,
  signal,
  eventTarget = window,
  now = Date.now,
  closedPollMs = 350,
}) {
  return new Promise((resolve, reject) => {
    const registration = accountLinkRegistration({
      type: 'moebius:account-link-register',
      authorizationOrigin: attempt.authorizationOrigin,
      state: attempt.state,
      expiresAt: attempt.expiresAt,
    }, now)
    let settled = false
    let closedTimer = null
    let expiryTimer = null

    const finish = (error, result) => {
      if (settled) return
      settled = true
      eventTarget.removeEventListener('message', receive)
      signal?.removeEventListener('abort', abort)
      clearInterval(closedTimer)
      clearTimeout(expiryTimer)
      try { popup?.close?.() } catch { /* a cross-origin popup may already be gone */ }
      if (error) reject(error)
      else resolve(result)
    }
    function receive(event) {
      const completion = accountLinkCompletion(event, registration, now)
      if (completion) finish(null, { code: completion.code, state: completion.state })
    }
    function abort() { finish(new AccountLinkError('cancelled')) }

    if (!registration) {
      finish(new AccountLinkError('invalid', 'Möbius returned an invalid sign-in address.'))
      return
    }
    if (signal?.aborted) {
      abort()
      return
    }
    eventTarget.addEventListener('message', receive)
    signal?.addEventListener('abort', abort, { once: true })
    closedTimer = setInterval(() => {
      if (popup.closed) finish(new AccountLinkError('closed'))
    }, closedPollMs)
    expiryTimer = setTimeout(() => finish(new AccountLinkError('timeout')),
      Math.max(0, registration.deadline - now()))
    try {
      popup.location.replace(attempt.authorizationUrl)
    } catch {
      finish(new AccountLinkError('navigation'))
    }
  })
}

/**
 * Finish a link with the code the account returned. Resolves
 * {status:'linked', identity} once linking is confirmed — directly, or by
 * re-reading the account after a lost response — or
 * {status:'unconfirmed', completion, message} when a retry may still finish
 * it. Any other refusal rejects with the service's message.
 */
export async function completeAccountLink(completion, { request = mobiusAccessRequest } = {}) {
  try {
    const identity = await request('/api/identity/link/complete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(completion),
    })
    return { status: 'linked', identity }
  } catch (error) {
    let current = null
    try {
      current = await request('/api/identity')
    } catch { /* keep the original completion error */ }
    if (current?.account_mode === 'linked' && !current.account_unavailable) {
      return { status: 'linked', identity: current }
    }
    if (retryableAccessError(error)) {
      return { status: 'unconfirmed', completion, message: ACCOUNT_LINK_MESSAGES.unconfirmed }
    }
    throw error
  }
}

/**
 * Run the whole sign-in from a click handler. The popup opens before the
 * first await, so call this synchronously inside the click. `onCompleting`
 * fires once the account has approved; from then on `signal` no longer
 * cancels, so a hidden success can never race a dismissed sign-in.
 */
export async function linkMobiusAccount({
  provider,
  signal,
  onCompleting,
  openWindow = (...args) => window.open(...args),
  request = mobiusAccessRequest,
  eventTarget = window,
  now = Date.now,
  closedPollMs,
}) {
  if (!ACCOUNT_LINK_PROVIDERS.includes(provider)) throw new AccountLinkError('invalid', 'Choose Google or Apple to sign in.')
  const popup = openWindow('about:blank', POPUP_NAME, POPUP_FEATURES)
  if (!popup) throw new AccountLinkError('blocked')
  let attempt
  try {
    if (signal?.aborted) throw new AccountLinkError('cancelled')
    attempt = parseLinkAttempt(await request('/api/identity/link/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider }),
      signal,
    }))
  } catch (error) {
    try { popup.close() } catch { /* already gone */ }
    if (signal?.aborted) throw new AccountLinkError('cancelled')
    throw error
  }
  const result = await awaitAccountLinkCompletion({ popup, attempt, signal, eventTarget, now, closedPollMs })
  onCompleting?.()
  return completeAccountLink({ code: result.code, state: result.state, attempt: attempt.attempt }, { request })
}
