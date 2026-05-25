/**
 * Fetch wrapper that attaches the JWT token and handles 401 responses.
 * BASE strips the trailing slash from Vite's BASE_URL so paths like
 * /api/chats work regardless of deployment prefix (e.g. /proxy/8001/).
 */
import { del as idbDel } from 'idb-keyval'

export const BASE = (import.meta.env.BASE_URL || '/').replace(/\/$/, '')

// localStorage access can throw in private-browsing modes or when the
// storage quota is hit. App.jsx reads getToken() during initial render
// to decide between Shell / Login / SetupWizard — an uncaught throw
// here would crash the splash. Wrap all three helpers defensively.
export function getToken() {
  try { return localStorage.getItem('token') } catch { return null }
}

export function setToken(token) {
  try { localStorage.setItem('token', token) } catch {}
}

export function clearToken() {
  try { localStorage.removeItem('token') } catch {}
  // Setup-wizard resume state assumes an active token. If the token
  // is gone (logout / expiry), clear the resume key so the user
  // doesn't get bounced back into the wizard after they re-login.
  try { localStorage.removeItem('setup-step') } catch {}
}

// Wipes persisted client state on logout / token expiry: the
// TanStack Query cache (IndexedDB) AND the SW Cache Storage
// entries. The SW currently caches `mobius-vendor-*`,
// `mobius-assets-*`, `mobius-proxy-*`, and `mobius-esm-*` —
// nothing per-owner anymore (app frame/module moved to HTTP
// ETag revalidation, no SW cache entry, see sw.js), but the wipe
// still matters because:
//   - the TanStack Query cache (IDB) holds owner-scoped chat/app
//     lists that a subsequent owner on a shared device must not see;
//   - cached `mobius-proxy-*` responses may include owner-fetched
//     remote assets that look benign but expose visited URLs;
//   - bumping past a stale SW that still has the retired
//     `mobius-apps-*` cache (pre-v6 instances) needs the activate
//     purge to land, and an explicit wipe forces it on logout
//     rather than waiting for the next navigation.
// Returns a promise so callers can `await` it before reloading the
// page (otherwise the browser would abort the in-flight delete).
export function clearQueryCache() {
  return Promise.all([
    idbDel('mobius-query-cache').catch(() => {}),
    wipeSwCaches().catch(() => {}),
  ])
}

async function wipeSwCaches() {
  if (typeof caches === 'undefined') return
  const keys = await caches.keys()
  await Promise.all(
    keys.filter(k => k.startsWith('mobius-')).map(k => caches.delete(k))
  )
}

let _setupInProgress = false
export function setSetupInProgress(v) { _setupInProgress = v }

export async function apiFetch(path, options = {}) {
  const token = getToken()
  const headers = {
    'Content-Type': 'application/json',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...options.headers,
  }

  const res = await fetch(`${BASE}/api${path}`, { ...options, headers })

  if (res.status === 401 && !_setupInProgress) {
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

export const api = {
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
  },
  apps: {
    list: () => apiFetch('/apps/'),
    // Stable per-app URL — cache freshness is handled by the server's
    // ETag + the browser's HTTP cache, not by a manual `?v=` param.
    // See backend/app/routes/apps.py for the ETag derivation. The
    // iframe REMOUNTS on app_updated (via the React `key` prop in
    // AppCanvas) which forces the browser to re-fetch and revalidate
    // via If-None-Match.
    frameUrl: (appId) => `${BASE}/api/apps/${appId}/frame`,
  },
  settings: {
    get: () => apiFetch('/settings'),
    save: (payload) => apiFetch('/settings', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  },
  theme: {
    get: () => apiFetch('/theme'),
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
  push: {
    vapidKey: () => apiFetch('/push/vapid-key'),
    subscribe: (payload) => apiFetch('/push/subscribe', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  },
}
