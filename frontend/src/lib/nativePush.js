// Native push bridge for the Möbius mobile app.
//
// The app injects `window.MobiusNative` into the top frame on the owner's own
// origin (never in mini-app iframes, never in a plain browser). Its
// `registerPush()` raises the OS permission prompt the first time and resolves
// to `{ token, ... }`, or `null` when push is denied or unavailable. After one
// call in a page, a rotated token arrives as a `mobius:push-token` event. The
// server sends through the mobius.you push relay; tapping a notification opens
// its `url` in the app.
//
// Nothing is registered until the owner turns phone notifications on in
// Settings (a tap, since the first call can show the OS permission prompt).
// After that, each load re-registers silently, which keeps the relay binding
// fresh and re-arms rotation. Turning it off in Settings, or signing out,
// forgets the phone while the credential still works.

import { api } from '../api/client.js'

// The last token the server accepted, so turning off or signing out knows
// what to forget.
const TOKEN_KEY = 'mobius.nativePush.token'
// Set only by the owner's Turn on in Settings; cleared by Turn off and sign-out.
const ENABLED_KEY = 'mobius.nativePush.enabled'
// Earlier tokens of this phone that still have to be forgotten. Kept until the
// server confirms, so a failed cleanup is retried instead of leaving a second
// registration that duplicates every notification.
const STALE_KEY = 'mobius.nativePush.staleTokens'
const MAX_STALE = 10

let listeningOn = null
let pending = Promise.resolve()
let signedOut = false

function read(key) {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function write(key, value) {
  try {
    if (value) localStorage.setItem(key, value)
    else localStorage.removeItem(key)
  } catch {
    // Without storage the choice lasts only for this page.
  }
}

const storedToken = () => read(TOKEN_KEY)

function staleTokens() {
  try {
    const list = JSON.parse(read(STALE_KEY) || '[]')
    return Array.isArray(list) ? list.filter((token) => typeof token === 'string' && token) : []
  } catch {
    return []
  }
}

function writeStale(list) {
  write(STALE_KEY, list.length ? JSON.stringify(list.slice(-MAX_STALE)) : null)
}

// Forgets earlier tokens; any the server did not confirm stay for next time.
async function forgetStale(push) {
  const left = []
  for (const token of staleTokens()) {
    try {
      const response = await push.devices.forget({ token })
      if (!response?.ok) left.push(token)
    } catch {
      left.push(token)
    }
  }
  writeStale(left)
}

export function isNativePushEnabled() {
  return read(ENABLED_KEY) === '1'
}

async function failure(response, fallback) {
  // apiFetch resolves a non-2xx Response; the server's detail says why (for
  // example, the account still needs linking).
  try {
    const { detail } = await response.json()
    if (typeof detail === 'string' && detail) return detail
  } catch {
    // Fall through to the generic message.
  }
  return fallback
}

// Registers one token with the server; resolves to null or an error message.
function save(result, push) {
  const token = result?.token
  if (signedOut || typeof token !== 'string' || !token) return Promise.resolve(null)
  // Serialized, so turning off or signing out waits for a registration in flight.
  const run = pending.catch(() => {}).then(async () => {
    if (signedOut) return null
    const previous = storedToken()
    const response = await push.devices.register({ token })
    if (response?.ok) {
      write(TOKEN_KEY, token)
      if (previous && previous !== token) {
        // The phone got a new push token; the old one may keep working for a
        // while, so stop sending to it or every notification arrives twice.
        writeStale([...staleTokens().filter((stale) => stale !== token), previous])
      }
      await forgetStale(push)
      return null
    }
    return failure(response, "Couldn't turn on notifications for this phone. Try again.")
  })
  pending = run
  return run
}

function listenForRotation(win, push) {
  if (listeningOn === win) return
  listeningOn = win
  win.addEventListener('mobius:push-token', (event) => {
    if (isNativePushEnabled()) save(event?.detail, push).catch(() => {})
  })
}

export function isNativePushAvailable(win = globalThis.window) {
  return typeof win?.MobiusNative?.registerPush === 'function'
}

/** Whether a registered device (by its token's last characters) is this phone. */
export function isThisPhone(tokenTail) {
  const token = storedToken()
  return !!token && !!tokenTail && token.endsWith(tokenTail)
}

/**
 * Shell start: re-register silently, but only if the owner turned phone
 * notifications on here. Also ends an earlier sign-out in the same page.
 */
export async function registerNativePush({ win = globalThis.window, push = api.push } = {}) {
  signedOut = false
  if (!isNativePushAvailable(win) || !isNativePushEnabled()) return
  listenForRotation(win, push)
  await save(await win.MobiusNative.registerPush(), push)
}

/**
 * Settings → Turn on. Call from a tap: it can show the OS prompt.
 * Resolves to `{ status: 'on' | 'denied' | 'error' | 'cancelled', message? }`;
 * `cancelled` means the owner signed out meanwhile.
 */
export async function enableNativePush({ win = globalThis.window, push = api.push } = {}) {
  if (!isNativePushAvailable(win)) return { status: 'error', message: 'Only available in the Möbius app.' }
  signedOut = false
  try {
    const result = await win.MobiusNative.registerPush()
    if (signedOut) return { status: 'cancelled' }
    if (!result) return { status: 'denied' }
    listenForRotation(win, push)
    const error = await save(result, push)
    // Signing out while this was in flight wins: the choice is not recorded.
    if (signedOut) return { status: 'cancelled' }
    if (error) return { status: 'error', message: error }
    write(ENABLED_KEY, '1')
    return { status: 'on' }
  } catch {
    return { status: 'error', message: "Couldn't turn on notifications for this phone. Try again." }
  }
}

/**
 * Settings → Turn off. Stops notifications to this phone; the OS permission
 * is untouched. Resolves to null or an error message (the token is kept so
 * Turn off can be tried again).
 */
export async function disableNativePush({ push = api.push } = {}) {
  write(ENABLED_KEY, null)
  await pending.catch(() => {})
  await forgetStale(push)
  const token = storedToken()
  if (!token) return null
  try {
    const response = await push.devices.forget({ token })
    if (!response?.ok) return failure(response, "Couldn't turn off notifications for this phone. Try again.")
  } catch {
    return "Couldn't reach Möbius. Try again."
  }
  write(TOKEN_KEY, null)
  return null
}

/** Sign-out: stop notifications to this phone while the credential works. */
export async function forgetNativePush({ push = api.push } = {}) {
  signedOut = true
  await disableNativePush({ push })
}

export function resetNativePushForTests() {
  listeningOn = null
  pending = Promise.resolve()
  signedOut = false
}
