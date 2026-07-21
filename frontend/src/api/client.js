/**
 * Fetch wrapper that attaches the JWT token and handles 401 responses.
 * BASE strips the trailing slash from Vite's BASE_URL so paths like
 * /api/chats work regardless of deployment prefix (e.g. /proxy/8001/).
 */
import { del as idbDel } from 'idb-keyval'
import * as setupSession from '../lib/setupSession.js'
import { clearLatchedTokens } from '../lib/appToken.js'
import { clearOwnerDraftStorage } from '../lib/ownerDraftStorage.js'
import { verifyConnectivity } from '../lib/connectivityStore.js'

export const BASE = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')

// The opaque embedded-chat document must never read or receive the owner's
// browser token. App.jsx enables this mode before ChatEmbed mounts; the only
// credential exposed through getToken() is then the short-lived chat session
// established by the server-verified bootstrap exchange.
let ephemeralAuthEnabled = false
let ephemeralToken = null
let ephemeralInstanceId = null
let ephemeralSessionGeneration = 0

export function beginEphemeralAuth() {
  ephemeralAuthEnabled = true
}

export function setEphemeralAuthSession(token, instanceId) {
  if (!ephemeralAuthEnabled) throw new Error('Ephemeral auth is not enabled')
  const nextToken = token || null
  const nextInstanceId = instanceId || null
  if (nextToken !== ephemeralToken || nextInstanceId !== ephemeralInstanceId) {
    ephemeralSessionGeneration += 1
  }
  ephemeralToken = nextToken
  ephemeralInstanceId = nextInstanceId
}

export function clearEphemeralAuthSession() {
  if (ephemeralToken !== null || ephemeralInstanceId !== null) {
    ephemeralSessionGeneration += 1
  }
  ephemeralToken = null
  ephemeralInstanceId = null
}

// Media credentials minted by an embedded chat are chained to the exact
// chat_embed session. This memory-only generation lets mediaToken.js replace
// its per-chat cache entry atomically when that session changes, without
// exposing or decoding the bearer itself.
export function getAuthSessionCacheKey() {
  return ephemeralAuthEnabled ? `embed:${ephemeralSessionGeneration}` : 'owner'
}

export function isEphemeralAuth() {
  return ephemeralAuthEnabled
}

// localStorage access can throw in private-browsing modes or when the
// storage quota is hit. App.jsx reads getToken() during initial render
// to decide between Shell / Login / SetupWizard — an uncaught throw
// here would crash the splash. Wrap all three helpers defensively.
export function getToken() {
  if (ephemeralAuthEnabled) return ephemeralToken
  try { return localStorage.getItem('token') } catch { return null }
}

export function getAuthHeaders(extra = {}) {
  const token = getToken()
  return {
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(ephemeralAuthEnabled && ephemeralInstanceId
      ? { 'X-Mobius-Embed-Instance': ephemeralInstanceId }
      : {}),
    ...extra,
  }
}

export function setToken(token) {
  try { localStorage.setItem('token', token) } catch {}
}

export function clearToken() {
  if (ephemeralAuthEnabled) {
    clearEphemeralAuthSession()
    return
  }
  try { localStorage.removeItem('token') } catch {}
  // Setup-wizard resume state assumes an active token. If the token
  // is gone (logout / expiry), clear the resume key + in-progress
  // flag so the user doesn't get bounced back into the wizard after
  // they re-login.
  setupSession.clearResumeStep()
  setupSession.setInProgress(false)
  // The walkthrough's local-completion flag is keyed by browser, not
  // by owner. Without this clear, a logout + new-owner setup would
  // hide the walkthrough for the new account AND trigger a
  // reconciliation POST against the new owner (see fetchWalkthrough
  // in hooks/queries.js — server-completed=false + localCompleted=true
  // fires `/owner/walkthrough/complete`). Both are wrong: the new
  // owner hasn't seen the walkthrough, and the server stamp should
  // reflect their own dismissal, not a stale browser flag.
  try { localStorage.removeItem('mobius:walkthrough-completed') } catch {}
}

// Wipes persisted client state on logout / token expiry: the
// TanStack Query cache (IndexedDB) AND the SW Cache Storage
// entries. Two cache-name prefixes need clearing now:
//   - `mobius-*` — runtime caches registered in src/sw.js
//     (`mobius-vendor`, `mobius-esm`, `mobius-proxy`) plus any
//     pre-vite-plugin-pwa legacy names that lingered.
//   - `workbox-*` — precache entries injected by vite-plugin-pwa
//     (`workbox-precache-v2-<scope>`) plus the workbox-runtime
//     bucket. These hold the shell bundle, manifest, and icons —
//     not owner-scoped data but worth purging so the next owner
//     on a shared device gets a clean install on next visit.
// The TanStack Query cache (IDB) holds owner-scoped chat/app
// lists; that's the primary privacy reason for the wipe. Returns
// a promise so callers can `await` it before reloading the page
// (otherwise the browser would abort the in-flight delete).
export function clearQueryCache() {
  // Owner-scoped in-memory state: the AppCanvas token latch holds resolved
  // app/owner tokens across iframe remounts; drop it on logout so a remount
  // after the session ends can't reuse the previous owner's token.
  try { clearLatchedTokens() } catch {}
  // Composer text and question choices are owner-authored content. Unlike
  // harmless shell preferences, they must not survive logout/token expiry and
  // appear in a later owner's session on the same browser.
  clearOwnerDraftStorage()
  // Media token cache is per-owner (tokens carry the owner's epoch). Clear
  // on logout so a new session doesn't inherit stale media tokens.
  try {
    import('./mediaToken.js').then(m => m.clearMediaTokenCache()).catch(() => {})
  } catch {}
  return Promise.all([
    idbDel('mobius-query-cache').catch(() => {}),
    delOutboxDb().catch(() => {}),
    delDatabase('mobius-signals', 'signal queue').catch(() => {}),
    wipeSwCaches().catch(() => {}),
  ])
}

// Remove browser-local queues and mirrors after an explicit app-data wipe.
// Soft uninstall intentionally preserves these records so Undo can restore
// offline work. The runtime owns the IndexedDB schemas, so keep the record
// traversal there rather than duplicating it in bundled code.
export async function clearAppRuntimeData(appId) {
  try {
    const runtimeUrl = `${BASE}/mobius-runtime.js`
    const runtime = await import(/* @vite-ignore */ runtimeUrl)
    await runtime.purgeAppRuntimeData?.(appId)
  } catch {
    // The server-side wipe already succeeded. Local cleanup is best-effort and
    // the rotated installation nonce still prevents stale record reuse.
  }
}

// The offline outbox and signal queue (mobius-runtime.js) are their OWN
// IndexedDB databases, not idb-keyval keys — so both must be dropped with
// deleteDatabase, not idbDel. The outbox holds queued writes and the read-through cache
// mirror (the `cache` store in the same DB), both owner-scoped — clearing it
// on logout keeps the next owner on a shared device from inheriting either.
// `onblocked` does NOT mean done: an open runtime connection is holding the DB.
// The runtime now closes its handles per-transaction and on `versionchange`,
// so a delete should not stay blocked; if it does we still resolve (logout must
// not hang) but warn, rather than silently claiming a clean wipe.
function delOutboxDb() {
  return delDatabase('mobius-outbox', 'outbox')
}

function delDatabase(name, label) {
  return new Promise((resolve) => {
    try {
      const req = indexedDB.deleteDatabase(name)
      req.onsuccess = req.onerror = () => resolve()
      req.onblocked = () => {
        // eslint-disable-next-line no-console
        console.warn(`mobius: ${label} DB delete blocked by an open connection on logout`)
        resolve()
      }
    } catch {
      resolve()
    }
  })
}

async function wipeSwCaches() {
  if (typeof caches === 'undefined') return
  const keys = await caches.keys()
  await Promise.all(
    keys
      .filter(k => k.startsWith('mobius-') || k.startsWith('workbox-'))
      .map(k => caches.delete(k))
  )
}

export async function apiFetch(path, options = {}) {
  const headers = {
    'Content-Type': 'application/json',
    ...getAuthHeaders(options.headers),
  }
  const sentCredential = Object.entries(headers).some(
    ([name, value]) => name.toLowerCase() === 'authorization' && !!value,
  )

  // Opt-in timeout: callers that must not hang forever (e.g. the background
  // reconcile poll and the message fetch — see ChatView) pass `timeoutMs`.
  // Existing callers omit it and keep the un-timed behaviour, so this can't
  // regress a legitimately-slow endpoint. A caller-supplied `signal` wins.
  const { timeoutMs, ...fetchOptions } = options
  let signal = fetchOptions.signal
  let timeoutTimer
  if (timeoutMs && !signal) {
    const ctrl = new AbortController()
    timeoutTimer = setTimeout(() => ctrl.abort(), timeoutMs)
    signal = ctrl.signal
  }

  let res
  try {
    res = await fetch(`${BASE}/api${path}`, { ...fetchOptions, headers, signal })
  } catch (error) {
    // The request is evidence, not a verdict. Ask the shared reachability store
    // to verify promptly; its hysteresis still prevents one transient failure
    // from flapping every retained chat offline.
    void verifyConnectivity()
    throw error
  } finally {
    if (timeoutTimer) clearTimeout(timeoutTimer)
  }

  if (res.status === 401 && sentCredential && !setupSession.isInProgress()) {
    if (ephemeralAuthEnabled) {
      clearEphemeralAuthSession()
      window.dispatchEvent(new CustomEvent('mobius:chat-embed-auth-expired'))
      throw new Error('EMBED_AUTH_EXPIRED')
    }
    clearToken()
    try { sessionStorage.setItem('auth_expired', '1') } catch {}
    // Await the cache wipe before reloading. Without this, the page
    // reload aborts the IndexedDB delete and the next owner could see
    // stale chats/messages from the cached query data.
    await clearQueryCache()
    // Defer reload one tick and throw a typed error so callers'
    // try/catch/finally blocks run (stopping spinners) before the
    // page goes away. Previously we returned a never-resolving
    // promise, which left finally{} clauses dangling for the entire
    // reload window — visible as stuck loading state.
    setTimeout(() => window.location.reload(), 100)
    throw new Error('AUTH_EXPIRED')
  }

  return res
}

/**
 * Decode a JSON API response at the client boundary. Endpoints that expose a
 * data-object contract (rather than the raw Fetch Response contract used by
 * most existing query hooks) should use this helper so callers cannot confuse
 * Response fields such as `url` with fields from the response body.
 */
export async function jsonOrThrow(response, label = 'Request failed') {
  let body = null
  try {
    body = await response.json()
  } catch {
    if (response.ok) throw new Error(`${label}: invalid JSON response`)
  }
  if (!response.ok) {
    throw new Error(body?.detail || `${label} (${response.status})`)
  }
  return body
}

// The platform's DELETION-EVIDENCE CONTRACT. A resource missing from a LIST read is
// only a HINT, never proof it was deleted: the /api/{chats,apps,...}/ list routes are
// NetworkFirst (sw.js), so a slow or offline read can resolve from a stale SW cache
// fallback that is byte-indistinguishable from a live response — and a filtered list
// (e.g. /api/chats hides app-attributed chats) or a lagging list can omit a live one.
// The ONLY authoritative deletion evidence is a direct per-resource GET returning 404
// (the backend's live_*_or_404 tombstone). This helper classifies one such probe so
// every caller reads the contract the same way instead of re-deriving it:
//   'deleted' — a real 404: the resource is genuinely gone; safe to tear it down.
//   'exists'  — a 2xx: present, merely off the filtered/lagging list; keep it.
//   'unknown' — any other status, or a network / timeout / offline / auth error: NOT
//               deletion evidence, so the caller must leave the resource alone.
// It owns only "what counts as gone"; each caller owns its own stale-guard + teardown.
export async function probeDeletion(path) {
  try {
    const res = await apiFetch(path, { timeoutMs: 15000 })
    if (res.status === 404) return 'deleted'
    if (res.ok) return 'exists'
    return 'unknown'
  } catch {
    return 'unknown'
  }
}

export const api = {
  // Public build identity. Used by Settings to show the served platform build
  // and frontend bundle identity.
  version: () => apiFetch('/version'),
  auth: {
    /**
     * Login runs before any JWT exists, so the auth interceptor adds no
     * Authorization header. It still goes through apiFetch so the path
     * and base-prefix logic stay centralized.
     */
    login: ({ username, password }) => apiFetch('/auth/token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ username, password }),
    }),
    setup: {
      status: () => apiFetch('/auth/setup/status'),
      create: (payload) => apiFetch('/auth/setup', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
    },
    provider: {
      statuses: () => apiFetch('/auth/providers/status'),
      appToken: (appId) => apiFetch('/auth/app-token', {
        method: 'POST',
        body: JSON.stringify({ app_id: appId }),
      }),
      claude: {
        status: () => apiFetch('/auth/provider/status'),
        startLogin: () => apiFetch('/auth/provider/login', { method: 'POST' }),
        submitCode: (code) => apiFetch('/auth/provider/code', {
          method: 'POST',
          body: JSON.stringify({ code }),
        }),
      },
      codex: {
        startLogin: () => apiFetch('/auth/provider/codex/login', { method: 'POST' }),
        status: () => apiFetch('/auth/provider/codex/status'),
      },
    },
  },
  chats: {
    list: () => apiFetch('/chats'),
    create: (payload) => apiFetch('/chats', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
    detail: (chatId, { limit } = {}) => {
      const params = new URLSearchParams()
      if (limit !== undefined) params.set('limit', String(limit))
      const query = params.toString()
      return apiFetch(`/chats/${chatId}${query ? `?${query}` : ''}`)
    },
    update: (chatId, payload) => apiFetch(`/chats/${chatId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
    remove: (chatId) => apiFetch(`/chats/${chatId}`, { method: 'DELETE' }),
    recover: (chatId) => apiFetch(`/chats/${chatId}/recover`, { method: 'POST' }),
  },
  apps: {
    list: () => apiFetch('/apps/'),
    remove: (appId) => apiFetch(`/apps/${appId}`, { method: 'DELETE' }),
    recover: (appId) => apiFetch(`/apps/${appId}/recover`, { method: 'POST' }),
    // Wipes the app's runtime storage back to empty while KEEPING it
    // installed — distinct from `remove` (which tombstones the whole app).
    deleteData: (appId) => apiFetch(`/apps/${appId}/data`, { method: 'DELETE' }),
    // Stable base URL. AppCanvas appends `?v=<app.updated_at>` so the
    // service worker can serve cached offline-capable apps cache-first while
    // app edits naturally become cache misses. The backend still sends ETags
    // for browser-cache revalidation on non-SW/cold paths.
    frameUrl: (appId) => `${BASE}/api/apps/${appId}/frame`,
    // AppCanvas fetches compiled code from the controlled shell document and
    // transfers it to the opaque frame. Keep the stable base URL here; the
    // broker appends the scoped token + versioned service-worker cache key.
    moduleUrl: (appId) => `${BASE}/api/apps/${appId}/module`,
  },
  services: {
    surface: async (slug) => jsonOrThrow(
      await apiFetch(`/local-services/${encodeURIComponent(slug)}/surface`),
      'Service surface request failed',
    ),
  },
  settings: {
    get: () => apiFetch('/settings'),
    save: (payload) => apiFetch('/settings', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  },
  models: {
    // Pass refresh=true to bypass the 5-minute server cache. The
    // manage-models modal's refresh button uses this; everything
    // else rides the cache.
    list: ({ refresh = false } = {}) => apiFetch(
      `/models${refresh ? '?refresh=true' : ''}`,
    ),
  },
  owner: {
    modelPrefs: {
      get: () => apiFetch('/owner/model-prefs'),
      save: (hiddenIds) => apiFetch('/owner/model-prefs', {
        method: 'PATCH',
        body: JSON.stringify({ hidden_ids: hiddenIds }),
      }),
    },
    walkthrough: {
      get: () => apiFetch('/owner/walkthrough'),
      // Idempotent — completion is a single bit. No body needed.
      complete: () => apiFetch('/owner/walkthrough/complete', {
        method: 'POST',
      }),
    },
  },
  theme: {
    get: () => apiFetch('/theme'),
    // Moves /data/shared/theme.css aside on the server so
    // DEFAULT_THEME paints again. The previous theme is preserved
    // as theme.css.reset-bak-<unix-ts> for rollback. Used by the
    // `?reset-theme=1` URL-parameter recovery flow in useTheme.
    reset: () => apiFetch('/theme/reset', { method: 'POST' }),
  },
  storage: {
    shared: {
      getThemeCss: () => apiFetch('/storage/shared/theme.css'),
      putThemeCss: (content) => apiFetch('/storage/shared/theme.css', {
        method: 'PUT',
        body: JSON.stringify({ content }),
      }),
      getThemeMode: () => apiFetch('/storage/shared/theme-mode'),
      putThemeMode: (mode) => apiFetch('/storage/shared/theme-mode', {
        method: 'PUT',
        body: JSON.stringify({ content: JSON.stringify(mode) }),
      }),
    },
  },
  notify: {
    send: (payload) => apiFetch('/notify', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  },
  admin: {
    restart: () => apiFetch('/admin/restart', { method: 'POST' }),
  },
  platform: {
    status: () => apiFetch('/platform/status'),
    check: () => apiFetch('/platform/check', { method: 'POST' }),
    // Read-only preview of the incoming update, shown for review before Apply.
    updatePreview: () => apiFetch('/platform/update-preview'),
    apply: () => apiFetch('/platform/apply', { method: 'POST' }),
    conflictResolverChat: () => apiFetch('/platform/conflict-resolver-chat', {
      method: 'POST',
    }),
    restart: () => apiFetch('/platform/restart', { method: 'POST' }),
  },
  push: {
    vapidKey: () => apiFetch('/push/vapid-key'),
    subscribe: (payload) => apiFetch('/push/subscribe', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  },
}
