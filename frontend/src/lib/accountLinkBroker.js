const ACCOUNT_LINK_WINDOW_MS = 10 * 60 * 1000
const ACCOUNT_LINK_STATE = /^[A-Za-z0-9_-]{32,512}$/

function loopback(hostname) {
  const host = hostname.toLowerCase()
  return host === 'localhost'
    || host.endsWith('.localhost')
    || host === '[::1]'
    || /^127(?:\.\d{1,3}){3}$/.test(host)
}

export function identityLinkBrokerAllowed(capabilityContract) {
  return capabilityContract?.data?.identity_manage === true
}

export function accountLinkRegistration(message, now = Date.now) {
  if (
    !message
    || typeof message !== 'object'
    || Array.isArray(message)
    || Object.keys(message).sort().join(',') !== 'authorizationOrigin,expiresAt,state,type'
    || message.type !== 'moebius:account-link-register'
  ) return null
  const state = typeof message.state === 'string' ? message.state : ''
  const authorizationOrigin = typeof message.authorizationOrigin === 'string'
    ? message.authorizationOrigin
    : ''
  const expiresAt = typeof message.expiresAt === 'string' ? message.expiresAt : ''
  if (!ACCOUNT_LINK_STATE.test(state) || !Number.isFinite(Date.parse(expiresAt))) {
    return null
  }
  let parsed
  try {
    parsed = new URL(authorizationOrigin)
  } catch {
    return null
  }
  if (
    parsed.origin !== authorizationOrigin
    || parsed.username
    || parsed.password
    || parsed.pathname !== '/'
    || parsed.search
    || parsed.hash
    || (parsed.protocol !== 'https:' && !(parsed.protocol === 'http:' && loopback(parsed.hostname)))
  ) {
    return null
  }
  return {
    state,
    authorizationOrigin,
    // The account backend enforces its own absolute expiry. Use a local bounded
    // lifetime here so a badly skewed browser clock cannot make a valid broker
    // registration vanish immediately or keep one alive indefinitely.
    deadline: now() + ACCOUNT_LINK_WINDOW_MS,
  }
}

export function accountLinkUnregistration(message, registration) {
  if (!registration || !message || typeof message !== 'object' || Array.isArray(message)) {
    return false
  }
  return Object.keys(message).sort().join(',') === 'state,type'
    && message.type === 'moebius:account-link-unregister'
    && message.state === registration.state
}

export function accountLinkCompletion(event, registration, now = Date.now) {
  if (!registration || now() > registration.deadline) return null
  if (event?.origin !== registration.authorizationOrigin) return null
  const message = event?.data
  if (!message || typeof message !== 'object' || Array.isArray(message)) return null
  if (
    Object.keys(message).sort().join(',') !== 'code,state,type'
    || message.type !== 'mobius-account-link'
    || message.state !== registration.state
    || typeof message.code !== 'string'
    || message.code.length < 16
    || message.code.length > 2048
  ) {
    return null
  }
  return {
    type: 'moebius:account-link-result',
    code: message.code,
    state: registration.state,
    authorizationOrigin: registration.authorizationOrigin,
  }
}
