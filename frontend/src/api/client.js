/**
 * Fetch wrapper that attaches the JWT token and handles 401 responses.
 * BASE strips the trailing slash from Vite's BASE_URL so paths like
 * /api/chats work regardless of deployment prefix (e.g. /proxy/8001/).
 */
import { del as idbDel } from 'idb-keyval'
import * as setupSession from '../lib/setupSession.js'
import { clearLatchedTokens } from '../lib/appToken.js'
import { clearOwnerDraftStorage } from '../lib/ownerDraftStorage.js'
import { clearReadingPositions } from '../components/ChatView/scroll/readingPositions.js'
import { clearDurableComposerDrafts } from '../components/ChatView/composerDraft.js'
import { clearChatOutbox } from '../components/ChatView/chatOutbox.js'
import {
  reportNetworkReachable,
  verifyConnectivity,
} from '../lib/connectivityStore.js'
import { SHELL_DATA_CACHE } from '../sw-cache-policy.js'

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
  // Reading positions became durable so they survive a PWA relaunch, which
  // also means they now outlive a session unless cleared here. Where the owner
  // had scrolled to in each conversation is owner-scoped, so it leaves with
  // the rest of their persisted state.
  try { clearReadingPositions() } catch {}
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
// lists; that's the primary privacy reason for the wipe. An expired-token path
// may preserve only the separately principal-partitioned chat outbox so the
// same owner can resume accepted intent after signing back in; explicit logout
// uses the default and removes it too. Returns
// a promise so callers can `await` it before reloading the page
// (otherwise the browser would abort the in-flight delete).
export function clearQueryCache({ preserveChatOutbox = false } = {}) {
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
    clearDurableComposerDrafts().catch(() => {}),
    idbDel('mobius-query-cache').catch(() => {}),
    delOutboxDb().catch(() => {}),
    ...(preserveChatOutbox
      ? []
      : [clearChatOutbox()]),
    delDatabase('mobius-signals', 'signal queue').catch(() => {}),
    wipeSwCaches().catch(() => {}),
  ])
}

// Remove browser-local queues and mirrors after an explicit app-data wipe.
// Soft uninstall intentionally preserves these records so Undo can restore
// offline work. The runtime owns the IndexedDB schemas, so keep the record
// traversal there rather than duplicating it in bundled code.
export async function clearAppRuntimeData(appId) {
  const cleanups = []
  try {
    const runtimeUrl = `${BASE}/mobius-runtime.js`
    const runtime = await import(/* @vite-ignore */ runtimeUrl)
    cleanups.push(runtime.purgeAppRuntimeData?.(appId))
  } catch {
    // The server-side wipe already succeeded. Local cleanup is best-effort and
    // the rotated installation nonce still prevents stale record reuse.
  }
  try {
    const deviceAssets = await import('../lib/deviceAssetCache.js')
    cleanups.push(deviceAssets.purgeDeviceAssetCache?.(appId))
  } catch {
    // Browser support and module loading are best-effort during data removal.
  }
  await Promise.allSettled(cleanups)
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
  // reconcile poll and message fetches — see ChatView) pass `timeoutMs`.
  // Compose it with a caller-owned lifecycle signal instead of making the two
  // mutually exclusive: a visible request is time-boxed, while hiding/unmounting
  // its owner can still release the connection immediately.
  const { timeoutMs, ...fetchOptions } = options
  let signal = fetchOptions.signal
  let timeoutTimer
  if (timeoutMs) {
    const ctrl = new AbortController()
    timeoutTimer = setTimeout(() => {
      const error = new Error('Request timed out')
      error.name = 'TimeoutError'
      ctrl.abort(error)
    }, timeoutMs)
    signal = signal ? AbortSignal.any([signal, ctrl.signal]) : ctrl.signal
  }

  let res
  try {
    res = await fetch(`${BASE}/api${path}`, { ...fetchOptions, headers, signal })
  } catch (error) {
    // The request is evidence, not a verdict. Ask the shared reachability store
    // to verify promptly; its hysteresis still prevents one transient failure
    // from flapping every retained chat offline. A caller-owned lifecycle abort
    // is not network evidence; checking connectivity for routine pane switches
    // would add needless requests. Our deadline aborts with TimeoutError and does
    // still enter the verification path.
    if (error?.name !== 'AbortError') void verifyConnectivity()
    throw error
  } finally {
    if (timeoutTimer) clearTimeout(timeoutTimer)
  }

  if (res.status === 401 && sentCredential && !setupSession.isInProgress()) {
    if (ephemeralAuthEnabled) {
      clearEphemeralAuthSession()
      window.dispatchEvent(new CustomEvent('mobius:ephemeral-auth-expired'))
      window.dispatchEvent(new CustomEvent('mobius:chat-embed-auth-expired'))
      throw new Error('EMBED_AUTH_EXPIRED')
    }
    clearToken()
    try { sessionStorage.setItem('auth_expired', '1') } catch {}
    // Await the cache wipe before reloading. Without this, the page
    // reload aborts the IndexedDB delete and the next owner could see
    // stale chats/messages from the cached query data.
    // Keep accepted chat intent through an expired credential. Each record is
    // partitioned by owner+epoch and can only replay after a matching login;
    // explicit Settings logout still uses the default full wipe.
    await clearQueryCache({ preserveChatOutbox: true })
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

// Shell-owned UI sometimes needs to invoke a generic app-attributed contract
// (for example, starting an app-owned agent handoff without navigating away).
// Keep that short-lived authority separate from apiFetch's OWNER-session 401
// handling: an expired app token must never sign the owner out.
async function appScopedFetch(path, appToken, options = {}) {
  if (!appToken) throw new Error('App authorization is unavailable')
  const headers = {
    'Content-Type': 'application/json',
    ...(options.headers || {}),
    Authorization: `Bearer ${appToken}`,
  }
  const response = await fetch(`${BASE}/api${path}`, { ...options, headers })
  reportNetworkReachable()
  return response
}

/**
 * Evict one offline shell projection after a confirmed list-affecting write.
 *
 * `/api/chats` and `/api/apps/` are NetworkFirst so the drawer remains useful
 * offline. That cache is intentionally allowed to be stale for a read, but a
 * successful mutation is stronger evidence: keeping the pre-write cached GET
 * lets the very next refetch resurrect a deleted row (or hide a recovered
 * one). CacheStorage is shared by every tab on this device, so this also
 * repairs their fallback layer. Best-effort by contract: a storage/quota
 * failure must never turn a committed server mutation into a client failure.
 */
export async function invalidateShellListCache(kind, {
  cacheStorage = typeof caches === 'undefined' ? null : caches,
  origin = typeof location === 'undefined' ? null : location.origin,
} = {}) {
  if (!cacheStorage || !origin) return false
  const pathname = kind === 'chats'
    ? `${BASE}/api/chats`
    : kind === 'apps'
      ? `${BASE}/api/apps/`
      : null
  if (!pathname) return false
  try {
    const cache = await cacheStorage.open(SHELL_DATA_CACHE)
    return await cache.delete(new URL(pathname, origin).href)
  } catch {
    return false
  }
}

async function listAffectingMutation(kind, path, options) {
  const response = await apiFetch(path, options)
  // A 404 on DELETE is authoritative too: the local projection is stale and
  // must not keep falling back to a cache that claims the row still exists.
  if (response.ok || (options?.method === 'DELETE' && response.status === 404)) {
    await invalidateShellListCache(kind)
  }
  return response
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
    const detail = body?.detail
    const message = typeof detail === 'string'
      ? detail
      : (detail?.message || `${label} (${response.status})`)
    const error = new Error(message)
    error.status = response.status
    error.detail = detail
    if (detail && typeof detail === 'object' && typeof detail.code === 'string') {
      error.code = detail.code
    }
    throw error
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
    sso: {
      // This is a top-level browser navigation, not an API fetch. The fetch
      // base may be a container-local address in production builds; the login
      // handoff must begin on the origin the owner is actually viewing.
      startUrl: (returnPath = '/') => (
        `/api/auth/sso/start?return_path=${encodeURIComponent(returnPath)}`
      ),
      consume: () => apiFetch('/auth/sso/session', { method: 'POST' }),
    },
    // One-time sign-in pass for an app being added to the iOS home screen,
    // where the new web app gets its own empty storage container. `mint`
    // needs the current session; `redeem` runs in the installed app, which
    // by definition has none yet.
    installPass: {
      mint: (slug) => apiFetch('/auth/install-pass', {
        method: 'POST',
        body: JSON.stringify({ slug }),
      }),
      redeem: (installPass, slug) => apiFetch('/auth/install-pass/redeem', {
        method: 'POST',
        body: JSON.stringify({ install_pass: installPass, slug }),
      }),
    },
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
    list: (options = {}) => apiFetch('/chats', options),
    search: (query, options = {}) => apiFetch(
      `/chats/search?q=${encodeURIComponent(query)}`,
      { timeoutMs: 10000, ...options },
    ),
    create: (payload, options = {}) => listAffectingMutation('chats', '/chats', {
      ...options,
      method: 'POST',
      body: JSON.stringify(payload),
    }),
    send: (chatId, payload, options = {}) => apiFetch(`/chats/${encodeURIComponent(chatId)}/messages`, {
      ...options,
      method: 'POST',
      body: JSON.stringify(payload),
    }),
    runtime: (chatId, options = {}) => apiFetch(
      `/chats/${encodeURIComponent(chatId)}/runtime`,
      options,
    ),
    goalPlan: (chatId, options = {}) => apiFetch(
      `/chats/${encodeURIComponent(chatId)}/goal-plan`,
      options,
    ),
    stop: (chatId, options = {}) => apiFetch('/chat/stop', {
      ...options,
      method: 'POST',
      body: JSON.stringify({ chat_id: chatId }),
    }),
    detail: (chatId, { limit, compact, anchor, signal, timeoutMs } = {}) => {
      const params = new URLSearchParams()
      if (limit !== undefined) params.set('limit', String(limit))
      if (compact !== undefined) params.set('compact', compact ? '1' : '0')
      if (anchor) params.set('anchor', String(anchor))
      const query = params.toString()
      return apiFetch(
        `/chats/${chatId}${query ? `?${query}` : ''}`,
        { signal, timeoutMs },
      )
    },
    currentUsage: (chatId, { provider, providerSessionId, signal } = {}) => {
      const params = new URLSearchParams({
        provider,
        provider_session_id: providerSessionId,
      })
      return apiFetch(
        `/chats/${encodeURIComponent(chatId)}/usage/current?${params}`,
        { signal },
      )
    },
    update: (chatId, payload) => listAffectingMutation('chats', `/chats/${chatId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
    // Chats and apps share one pinned section, so its order is one transaction
    // even though the rows live in two resource tables.
    reorderPinned: (items) => apiFetch('/chats/pinned-order', {
      method: 'PUT',
      body: JSON.stringify({ items }),
    }),
    remove: (chatId) => listAffectingMutation(
      'chats', `/chats/${chatId}`, { method: 'DELETE' },
    ),
    recover: (chatId) => listAffectingMutation(
      'chats', `/chats/${chatId}/recover`, { method: 'POST' },
    ),
    usage: (chatId, options = {}) => apiFetch(
      `/chats/${encodeURIComponent(chatId)}/usage`, options,
    ),
    usageSummary: (chatId, options = {}) => apiFetch(
      `/chats/${encodeURIComponent(chatId)}/usage?include_runs=false`, options,
    ),
  },
  appChats: {
    listWithToken: (appToken, { scope } = {}) => {
      const query = scope ? `?scope=${encodeURIComponent(scope)}` : ''
      return appScopedFetch(`/app-chats${query}`, appToken)
    },
    startWithToken: (appToken, payload) => appScopedFetch(
      '/app-chats/start', appToken, {
        method: 'POST',
        body: JSON.stringify(payload),
      },
    ),
  },
  secureInputs: {
    submit: (chatId, requestId, payload) => apiFetch(
      `/secure-inputs/${encodeURIComponent(chatId)}/${encodeURIComponent(requestId)}/submit`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  },
  apps: {
    list: (options = {}) => apiFetch('/apps/', options),
    sourceFiles: (appId, path = '', { signal, recursive = false } = {}) => apiFetch(
      `/apps/${encodeURIComponent(appId)}/source/files?path=${encodeURIComponent(path)}${recursive ? '&recursive=true' : ''}`,
      { signal },
    ),
    sourceGitStatus: (appId, { signal } = {}) => apiFetch(
      `/apps/${encodeURIComponent(appId)}/source/git/status`,
      { signal },
    ),
    sourceGitDiff: (appId, path, { signal } = {}) => apiFetch(
      `/apps/${encodeURIComponent(appId)}/source/git/diff?path=${encodeURIComponent(path)}`,
      { signal },
    ),
    readSourceFile: (appId, path, { download = false, signal } = {}) => apiFetch(
      `/apps/${encodeURIComponent(appId)}/source/file?path=${encodeURIComponent(path)}${download ? '&download=true' : ''}`,
      { signal },
    ),
    markOpened: (appId) => apiFetch(`/apps/${appId}/opened`, {
      method: 'POST',
    }),
    markActivitySeen: (appId, activityVersion) => apiFetch(`/apps/${appId}/activity/seen`, {
      method: 'POST',
      body: JSON.stringify({ activity_version: activityVersion }),
    }),
    markPreviewSeen: (appId, updatedAt, final = false) => apiFetch(`/apps/${appId}/preview/seen`, {
      method: 'POST',
      body: JSON.stringify({ updated_at: updatedAt, final }),
    }),
    update: (appId, payload) => listAffectingMutation('apps', `/apps/${appId}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
    publishHosted: (appId) => listAffectingMutation(
      'apps', `/apps/${appId}/hosted-publication`, { method: 'PUT' },
    ),
    stopHosted: (appId) => listAffectingMutation(
      'apps', `/apps/${appId}/hosted-publication`, { method: 'DELETE' },
    ),
    remove: (appId) => listAffectingMutation(
      'apps', `/apps/${appId}`, { method: 'DELETE' },
    ),
    recover: (appId) => listAffectingMutation(
      'apps', `/apps/${appId}/recover`, { method: 'POST' },
    ),
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
  projects: {
    list: () => apiFetch('/projects'),
    templates: () => apiFetch('/projects/templates'),
    legacy: () => apiFetch('/projects/legacy'),
    detail: (projectId) => apiFetch(`/projects/${encodeURIComponent(projectId)}`),
    markOpened: (projectId) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/opened`, { method: 'POST' },
    ),
    create: (payload) => apiFetch('/projects', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
    importLegacy: (payload) => apiFetch('/projects/import-legacy', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
    importGithub: (payload) => apiFetch('/projects/import-github', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
    redeemInvite: (payload) => apiFetch('/projects/invites/redeem', {
      method: 'POST', body: JSON.stringify(payload),
    }),
    collaboration: (projectId) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/collaboration`,
    ),
    heartbeat: (projectId) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/presence`, { method: 'POST' },
    ),
    createInvite: (projectId, payload) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/invites`, {
        method: 'POST', body: JSON.stringify(payload),
      },
    ),
    revokeInvite: (projectId, inviteId) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/invites/${encodeURIComponent(inviteId)}`,
      { method: 'DELETE' },
    ),
    updateMember: (projectId, memberId, payload) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(memberId)}`,
      { method: 'PATCH', body: JSON.stringify(payload) },
    ),
    revokeMember: (projectId, memberId) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/members/${encodeURIComponent(memberId)}`,
      { method: 'DELETE' },
    ),
    update: (projectId, payload) => apiFetch(`/projects/${encodeURIComponent(projectId)}`, {
      method: 'PATCH',
      body: JSON.stringify(payload),
    }),
    remove: (projectId) => apiFetch(`/projects/${encodeURIComponent(projectId)}`, {
      method: 'DELETE',
    }),
    recover: (projectId) => apiFetch(`/projects/${encodeURIComponent(projectId)}/recover`, {
      method: 'POST',
    }),
    chats: (projectId) => apiFetch(`/projects/${encodeURIComponent(projectId)}/chats`),
    agents: (projectId) => apiFetch(`/projects/${encodeURIComponent(projectId)}/agents`),
    agentMessages: (projectId, chatId) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/agent-messages?chat_id=${encodeURIComponent(chatId)}`,
    ),
    sendAgentMessage: (projectId, payload) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/agent-messages`, {
        method: 'POST', body: JSON.stringify(payload),
      },
    ),
    createChat: (projectId, payload) => apiFetch(`/projects/${encodeURIComponent(projectId)}/chats`, {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
    files: (projectId, path = '', { signal, recursive = false } = {}) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/files?path=${encodeURIComponent(path)}${recursive ? '&recursive=true' : ''}`,
      { signal },
    ),
    gitStatus: (projectId, { signal } = {}) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/git/status`,
      { signal },
    ),
    gitDiff: (projectId, path, { signal } = {}) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/git/diff?path=${encodeURIComponent(path)}`,
      { signal },
    ),
    initGit: (projectId) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/git/init`, { method: 'POST' },
    ),
    commitGit: (projectId, payload) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/git/commit`, {
        method: 'POST', body: JSON.stringify(payload),
      },
    ),
    remoteStatus: (projectId, { signal } = {}) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/git/remote`, { signal },
    ),
    connectRemote: (projectId, repository) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/git/remote`, {
        method: 'POST', body: JSON.stringify({ repository }),
      },
    ),
    fetchRemote: (projectId) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/git/fetch`, { method: 'POST' },
    ),
    pullRemote: (projectId, expectedHead) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/git/pull`, {
        method: 'POST', body: JSON.stringify({ expected_head: expectedHead || null }),
      },
    ),
    pushRemote: (projectId, expectedHead) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/git/push`, {
        method: 'POST',
        body: JSON.stringify({ expected_head: expectedHead || null, confirmed: true }),
      },
    ),
    readFile: (projectId, path, { download = false, signal } = {}) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/file?path=${encodeURIComponent(path)}${download ? '&download=true' : ''}`,
      { signal },
    ),
    changes: (projectId, after, { signal } = {}) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/changes${after == null ? '' : `?after=${encodeURIComponent(after)}`}`,
      { signal },
    ),
    workClaims: (projectId, { signal } = {}) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/work-claims`, { signal },
    ),
    claimWork: (projectId, payload) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/work-claim`, {
        method: 'PUT', body: JSON.stringify(payload),
      },
    ),
    releaseWork: (projectId, chatId = null) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/work-claim${chatId ? `?chat_id=${encodeURIComponent(chatId)}` : ''}`,
      { method: 'DELETE' },
    ),
    writeFile: (projectId, path, content, expectedRevision) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/file?path=${encodeURIComponent(path)}`,
      {
        method: 'PUT',
        body: JSON.stringify({
          content,
          ...(expectedRevision !== undefined
            ? { expected_revision: expectedRevision }
            : {}),
        }),
      },
    ),
    writeBytes: (projectId, path, bytes, expectedRevision = undefined) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/file-bytes?path=${encodeURIComponent(path)}`,
      {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/octet-stream',
          ...(expectedRevision === null
            ? { 'If-None-Match': '*' }
            : typeof expectedRevision === 'string'
              ? { 'If-Match': expectedRevision }
              : {}),
        },
        body: bytes,
      },
    ),
    createFolder: (projectId, path) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/folder`,
      { method: 'POST', body: JSON.stringify({ path }) },
    ),
    deleteFile: (projectId, path) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/file?path=${encodeURIComponent(path)}`,
      { method: 'DELETE' },
    ),
    downloadUrl: (projectId, path) => (
      `${BASE}/api/projects/${encodeURIComponent(projectId)}/file?path=${encodeURIComponent(path)}&download=true`
    ),
    // Rename or move a file/dir within the project tree. The backend confines
    // both paths, rejects symlink escape / dst-exists / into-descendant, and
    // maps an os.replace failure to a 4xx rather than a 500 (see the build spec).
    move: (projectId, { from_path, to_path }) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/move`,
      { method: 'POST', body: JSON.stringify({ from_path, to_path }) },
    ),
    // ── Artifacts (buildable outputs: website / latex) ───────────────────────
    artifacts: (projectId, { signal } = {}) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/artifacts`,
      { signal },
    ),
    createArtifact: (projectId, payload) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/artifacts`,
      { method: 'POST', body: JSON.stringify(payload) },
    ),
    deleteArtifact: (projectId, artifactId) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}`,
      { method: 'DELETE' },
    ),
    buildArtifact: (projectId, artifactId) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}/build`,
      { method: 'POST' },
    ),
    artifactLog: (projectId, artifactId) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}/log`,
    ),
    // Bytes of one confined output file (pdfjs fetches the latex pdf through
    // this via apiFetch so the owner Bearer authenticates the read).
    // Artifact output bytes always go through apiFetch with the Bearer header
    // (pdfjs for latex, and the shell fetching + inlining a website into a
    // sandboxed srcDoc). The owner token is NEVER placed in a URL a sandboxed
    // artifact could read from window.location.
    artifactOutput: (projectId, artifactId, path, { signal } = {}) => apiFetch(
      `/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(artifactId)}/output/${path.split('/').map(encodeURIComponent).join('/')}`,
      { signal },
    ),
  },
  services: {
    surface: async (slug) => jsonOrThrow(
      await apiFetch(`/local-services/${encodeURIComponent(slug)}/surface`),
      'Service surface request failed',
    ),
  },
  settings: {
    get: () => apiFetch('/settings'),
    providerUsage: (provider) => apiFetch(
      `/settings/provider-usage/${encodeURIComponent(provider)}`,
    ),
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
  screenControl: {
    start: async (payload) => jsonOrThrow(
      await apiFetch('/screen-control/sessions', {
        method: 'POST',
        body: JSON.stringify(payload),
      }),
      'Could not start screen control',
    ),
    respond: (sessionId, payload) => apiFetch(
      `/screen-control/sessions/${encodeURIComponent(sessionId)}/responses`,
      {
        method: 'POST',
        body: JSON.stringify(payload),
      },
    ),
    stop: (sessionId) => apiFetch(
      `/screen-control/sessions/${encodeURIComponent(sessionId)}`,
      { method: 'DELETE' },
    ),
  },
  notifications: {
    // Cursor pagination: `before` is the last row id of the previous page.
    list: ({ before, limit } = {}) => {
      const params = new URLSearchParams()
      if (before) params.set('before', String(before))
      if (limit) params.set('limit', String(limit))
      const qs = params.toString()
      return apiFetch(`/notifications${qs ? `?${qs}` : ''}`)
    },
    unreadCount: () => apiFetch('/notifications/unread-count'),
    // Seen-on-open: idempotent bulk mark-read (clears the bell badge).
    readAll: () => apiFetch('/notifications/read-all', { method: 'POST' }),
    // Owner action from the preview: remove all stored notifications.
    clearAll: () => apiFetch('/notifications', { method: 'DELETE' }),
  },
  admin: {
    restart: () => apiFetch('/admin/restart', { method: 'POST' }),
    rebuildStatus: () => apiFetch('/admin/rebuild'),
    rebuild: () => apiFetch('/admin/rebuild', { method: 'POST' }),
  },
  platform: {
    status: () => apiFetch('/platform/status'),
    check: () => apiFetch('/platform/check', { method: 'POST' }),
    // Read-only preview of the incoming update, shown for review before Apply.
    updatePreview: () => apiFetch('/platform/update-preview'),
    updateProgress: () => apiFetch('/platform/update-progress'),
    apply: (plan) => apiFetch('/platform/apply', {
      method: 'POST',
      body: JSON.stringify(plan),
    }),
    conflictResolverChat: () => apiFetch('/platform/conflict-resolver-chat', {
      method: 'POST',
    }),
    restart: () => apiFetch('/platform/restart', { method: 'POST' }),
  },
  // The chat card is a compact projection of the same reviewed ledger Contribute
  // owns. Its direct action calls the same guarded routes as the app; the card
  // never pushes or talks to GitHub itself.
  contributions: {
    forChat: (appId, chatId) => apiFetch(
      `/github/contributions/${appId}/for-chat/${encodeURIComponent(chatId)}`,
    ),
    coverageForChat: (appId, chatId, paths) => apiFetch(
      `/github/contributions/${appId}/for-chat/${encodeURIComponent(chatId)}/coverage`,
      {
        method: 'POST',
        body: JSON.stringify({ paths }),
      },
    ),
    publish: (appId, record, { autopilot = false } = {}) => {
      const update = record?.action === 'pr_update'
      const action = update ? 'update-existing' : 'submit'
      return apiFetch(
        `/github/contributions/${appId}/${encodeURIComponent(record.id)}/${action}`,
        {
          method: 'POST',
          body: JSON.stringify(update ? {} : {
            autopilot,
            submitter: 'chat-review-card',
          }),
        },
      )
    },
  },
  push: {
    vapidKey: () => apiFetch('/push/vapid-key'),
    subscribe: (payload) => apiFetch('/push/subscribe', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
    unsubscribe: (payload) => apiFetch('/push/subscribe', {
      method: 'DELETE',
      body: JSON.stringify(payload),
    }),
  },
}
