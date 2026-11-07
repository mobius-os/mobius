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
// Whether this phone gets notifications is the phone's own notification
// setting for the app; Möbius has no switch of its own. Each time the app
// opens, the page asks for the token: a token registers (or refreshes) this
// phone, and no token (notifications not allowed) forgets it, so the server
// keeps exactly the phones that can receive. Signing out forgets the phone
// while the credential still works.

import { api } from '../api/client.js'

// The last token the server accepted, so signing out knows what to forget.
const TOKEN_KEY = 'mobius.nativePush.token'
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
    return failure(response, "Couldn't connect this phone for notifications.")
  })
  pending = run
  return run
}

function listenForRotation(win, push) {
  if (listeningOn === win) return
  listeningOn = win
  win.addEventListener('mobius:push-token', (event) => {
    save(event?.detail, push).catch(() => {})
  })
}

export function isNativePushAvailable(win = globalThis.window) {
  return typeof win?.MobiusNative?.registerPush === 'function'
}

/** Forgets this phone's token at the server; resolves to null or an error message. */
async function forgetThisPhone(push) {
  await pending.catch(() => {})
  await forgetStale(push)
  const token = storedToken()
  if (!token) return null
  try {
    const response = await push.devices.forget({ token })
    if (!response?.ok) return failure(response, "Couldn't disconnect this phone. Try again.")
  } catch {
    return "Couldn't reach Möbius. Try again."
  }
  write(TOKEN_KEY, null)
  return null
}

/**
 * App open: connect this phone if its notification setting allows it, and
 * forget it if not. The first call may show the OS permission prompt, which
 * the app owns. Also ends an earlier sign-out in the same page.
 */
export async function registerNativePush({ win = globalThis.window, push = api.push } = {}) {
  signedOut = false
  if (!isNativePushAvailable(win)) return
  listenForRotation(win, push)
  const result = await win.MobiusNative.registerPush()
  // A sign-out while the app was answering wins: nothing registers.
  if (signedOut) return
  if (result?.token) await save(result, push)
  else await forgetThisPhone(push)
}

/** Sign-out: stop notifications to this phone while the credential works. */
export async function forgetNativePush({ push = api.push } = {}) {
  signedOut = true
  await forgetThisPhone(push)
}

export function resetNativePushForTests() {
  listeningOn = null
  pending = Promise.resolve()
  signedOut = false
}
