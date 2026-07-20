import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client.js'
import { appTokenRefreshInterval } from '../lib/appToken.js'

function jsonOrThrow(res, label) {
  if (!res.ok) throw new Error(`${label} ${res.status}`)
  return res.json()
}

async function jsonOrNull(res) {
  return res.ok ? res.json() : null
}

const themeKey = ['theme']
const themeModeKey = ['theme-mode']
const setupStatusKey = ['auth', 'setup', 'status']
const settingsKey = ['settings']
const appsKey = ['apps']
const chatsKey = ['chats']
const providerClaudeStatusKey = ['auth', 'provider', 'claude-status']
const providersStatusKey = ['auth', 'providers', 'status']
const modelRegistryKey = ['models', 'registry']
const modelPrefsKey = ['owner', 'model-prefs']
const walkthroughKey = ['owner', 'walkthrough']
const versionKey = ['version']

async function fetchTheme() {
  const res = await api.theme.get()
  return jsonOrThrow(res, 'theme fetch failed:')
}

function useThemeQuery() {
  return useQuery({
    queryKey: themeKey,
    queryFn: fetchTheme,
    staleTime: 60_000,
  })
}

async function fetchThemeMode() {
  const res = await api.storage.shared.getThemeMode()
  return jsonOrNull(res)
}

function useThemeModeQuery() {
  return useQuery({
    queryKey: themeModeKey,
    queryFn: fetchThemeMode,
    staleTime: 60_000,
  })
}

async function fetchSetupStatus() {
  const res = await api.auth.setup.status()
  return jsonOrThrow(res, 'setup status fetch failed:')
}

function useSetupStatusQuery({ enabled = true } = {}) {
  return useQuery({
    queryKey: setupStatusKey,
    queryFn: fetchSetupStatus,
    enabled,
    staleTime: 60_000,
    retry: 0,
  })
}

async function fetchSettings() {
  const res = await api.settings.get()
  return jsonOrThrow(res, 'settings fetch failed:')
}

function useSettingsQuery() {
  return useQuery({
    queryKey: settingsKey,
    queryFn: fetchSettings,
    // Settings is persisted to IndexedDB (see queryClient.js), so the
    // panel paints the cached provider config + CLI versions the instant
    // it opens — the hydrated cache is what gives the instant render. We
    // keep the default staleTime (0) so that instant paint is immediately
    // followed by a background refetch: true stale-while-revalidate. A
    // non-zero staleTime would suppress the on-mount refetch and let the
    // cached value sit stale for minutes, which is "cached but never
    // refreshes", not offline-first.
  })
}

async function fetchApps() {
  const res = await api.apps.list()
  const data = await jsonOrThrow(res, 'apps fetch failed:')
  return Array.isArray(data) ? data : []
}

function useAppsQuery() {
  return useQuery({
    queryKey: appsKey,
    queryFn: fetchApps,
  })
}

async function fetchChats() {
  const res = await api.chats.list()
  const data = await jsonOrThrow(res, 'chats fetch failed:')
  return Array.isArray(data) ? data : []
}

function useChatsQuery() {
  return useQuery({
    queryKey: chatsKey,
    queryFn: fetchChats,
  })
}

async function fetchAppToken(appId) {
  const res = await api.auth.provider.appToken(appId)
  const data = await jsonOrThrow(res, 'app-token fetch failed:')
  return data.token
}

function useAppTokenQuery(appId) {
  return useQuery({
    queryKey: ['app-token', appId],
    enabled: !!appId,
    queryFn: () => fetchAppToken(appId),
    staleTime: 5 * 60_000,
    // Mounted canvases stay alive in the shell's LRU for many hours. Refresh
    // before the app-token's JWT expiry even while the canvas is hidden, then
    // AppCanvas forwards the new token into the existing frame without remounting.
    refetchInterval: (query) => appTokenRefreshInterval(query.state.data),
    refetchIntervalInBackground: true,
    // Belt: server tokens expire at 8h. Cap gc below that expiry so a
    // re-opened app that sat idle overnight fetches a fresh token rather
    // than serving a long-cached (possibly expired) one. 7 hours keeps
    // the latch semantics and the offline-fallback path unchanged — the
    // latch store is keyed by appId+version and is not affected by gcTime.
    gcTime: 7 * 60 * 60_000,
  })
}

async function fetchClaudeProviderStatus() {
  const res = await api.auth.provider.statuses()
  const data = await jsonOrThrow(res, 'provider status fetch failed:')
  return data?.claude || { authenticated: false }
}

function useClaudeProviderStatusQuery({ enabled = true } = {}) {
  return useQuery({
    queryKey: providerClaudeStatusKey,
    queryFn: fetchClaudeProviderStatus,
    // Persisted alongside ['settings'] so the Settings panel's Claude
    // row paints its connected state from disk the instant it opens. We
    // keep the default staleTime (0) so that instant paint is followed
    // by a background refetch (stale-while-revalidate); explicit
    // invalidation (onClaudeAuthDone) still flips it after a re-auth.
    // `enabled` lets callers (SetupWizard) defer this until a token
    // exists — see call site for why.
    enabled,
  })
}

async function fetchProvidersStatus() {
  const res = await api.auth.provider.statuses()
  return jsonOrThrow(res, 'provider statuses fetch failed:')
}

function useProvidersStatusQuery({ enabled = true } = {}) {
  return useQuery({
    queryKey: providersStatusKey,
    queryFn: fetchProvidersStatus,
    enabled,
    // Cheap query whose data rarely changes within a session — refresh
    // tokens only flip after explicit re-auth or expiry on wake. The
    // useProviderAuthStatus hook invalidates on visibilitychange so a
    // wake-from-background triggers exactly one refetch; everything in
    // between rides this 5-minute cache.
    staleTime: 5 * 60_000,
  })
}

async function fetchModelRegistry() {
  const res = await api.models.list()
  const data = await jsonOrThrow(res, 'model registry fetch failed:')
  return data?.providers || {}
}

function useModelRegistryQuery({ enabled = true } = {}) {
  return useQuery({
    queryKey: modelRegistryKey,
    queryFn: fetchModelRegistry,
    enabled,
    // Mirror the server-side cache TTL so the client doesn't refetch
    // more often than upstream gives us new data. Refetches happen
    // through explicit invalidation (the manage-models modal's
    // refresh button) — not on every popover open.
    staleTime: 5 * 60_000,
  })
}

async function fetchModelPrefs() {
  const res = await api.owner.modelPrefs.get()
  const data = await jsonOrThrow(res, 'model prefs fetch failed:')
  return { hidden_ids: data?.hidden_ids || [] }
}

function useModelPrefsQuery({ enabled = true } = {}) {
  return useQuery({
    queryKey: modelPrefsKey,
    queryFn: fetchModelPrefs,
    enabled,
  })
}

async function fetchWalkthrough() {
  const res = await api.owner.walkthrough.get()
  const data = await jsonOrThrow(res, 'walkthrough status fetch failed:')
  // localStorage fallback: the WalkthroughOverlay writes this key on
  // dismiss alongside the optimistic-cache update, so even when the
  // POST to /owner/walkthrough/complete fails (flaky connection, tab
  // closed before persist) the next session still considers the user
  // onboarded. Server takes precedence — completed=true from either
  // side wins.
  let localCompleted = false
  try { localCompleted = localStorage.getItem('mobius:walkthrough-completed') === '1' } catch (_) {}
  const completed = !!data?.completed || localCompleted
  // Reconcile: if local says completed but server doesn't, fire a
  // background POST to land the persist that didn't make it last
  // time. Without this the localStorage write would be a one-way door
  // — Owner.walkthrough_completed_at would stay NULL forever, breaking
  // downstream onboarding analytics. Fire-and-forget; the next fetch
  // sees the updated server state OR the local flag still holds.
  if (localCompleted && !data?.completed) {
    api.owner.walkthrough.complete().catch(() => {})
  }
  return {
    completed,
    completed_at: data?.completed_at || null,
  }
}

function useWalkthroughQuery({ enabled = true } = {}) {
  return useQuery({
    queryKey: walkthroughKey,
    queryFn: fetchWalkthrough,
    enabled,
    // Walkthrough state never reverts (completion is monotonic), so
    // once we get a `completed: true` there's no point refetching.
    // The 24h staleTime is just a safety net for the rare case where
    // a sibling tab marks completion while this tab is open.
    staleTime: 24 * 60 * 60_000,
  })
}

async function fetchVersion() {
  const res = await api.version()
  return jsonOrThrow(res, 'version fetch failed:')
}

function useVersionQuery({ enabled = true } = {}) {
  return useQuery({
    queryKey: versionKey,
    queryFn: fetchVersion,
    enabled,
    // staleTime 0 so the "Check for updates" path always re-reads the live
    // build identity rather than a stale cached value.
    staleTime: 0,
  })
}

export const versionQueries = {
  current: {
    key: versionKey,
    fetch: fetchVersion,
    useQuery: useVersionQuery,
    invalidate: (queryClient) => queryClient.invalidateQueries({ queryKey: versionKey }),
  },
}

export const themeQueries = {
  keys: {
    all: themeKey,
    mode: themeModeKey,
  },
  fetch: fetchTheme,
  useQuery: useThemeQuery,
  invalidate: (queryClient, options = {}) => queryClient.invalidateQueries({ queryKey: themeKey, ...options }),
  mode: {
    key: themeModeKey,
    fetch: fetchThemeMode,
    useQuery: useThemeModeQuery,
    invalidate: (queryClient, options = {}) => queryClient.invalidateQueries({ queryKey: themeModeKey, ...options }),
  },
}

export const setupQueries = {
  status: {
    key: setupStatusKey,
    fetch: fetchSetupStatus,
    useQuery: useSetupStatusQuery,
    invalidate: (queryClient) => queryClient.invalidateQueries({ queryKey: setupStatusKey }),
  },
}

export const settingsQueries = {
  owner: {
    key: settingsKey,
    fetch: fetchSettings,
    useQuery: useSettingsQuery,
    invalidate: (queryClient) => queryClient.invalidateQueries({ queryKey: settingsKey }),
  },
}

export const appQueries = {
  keys: {
    all: appsKey,
    token: (appId) => ['app-token', appId],
  },
  list: {
    key: appsKey,
    fetch: fetchApps,
    useQuery: useAppsQuery,
    invalidate: (queryClient) => queryClient.invalidateQueries({ queryKey: appsKey }),
  },
  token: {
    key: (appId) => ['app-token', appId],
    fetch: fetchAppToken,
    useQuery: useAppTokenQuery,
    invalidate: (queryClient, appId) => queryClient.invalidateQueries({ queryKey: ['app-token', appId] }),
  },
}

export const chatQueries = {
  keys: {
    all: chatsKey,
    messages: (chatId) => ['chat-messages', chatId],
  },
  list: {
    key: chatsKey,
    fetch: fetchChats,
    useQuery: useChatsQuery,
    invalidate: (queryClient) => queryClient.invalidateQueries({ queryKey: chatsKey }),
  },
  messages: {
    key: (chatId) => ['chat-messages', chatId],
    remove: (queryClient, chatId) => queryClient.removeQueries({ queryKey: ['chat-messages', chatId] }),
  },
}

export const authQueries = {
  provider: {
    claudeStatus: {
      key: providerClaudeStatusKey,
      fetch: fetchClaudeProviderStatus,
      useQuery: useClaudeProviderStatusQuery,
      invalidate: (queryClient) => queryClient.invalidateQueries({ queryKey: providerClaudeStatusKey }),
    },
    statuses: {
      key: providersStatusKey,
      fetch: fetchProvidersStatus,
      useQuery: useProvidersStatusQuery,
      invalidate: (queryClient) => queryClient.invalidateQueries({ queryKey: providersStatusKey }),
    },
  },
}

export const modelQueries = {
  keys: {
    registry: modelRegistryKey,
    prefs: modelPrefsKey,
  },
  registry: {
    key: modelRegistryKey,
    fetch: fetchModelRegistry,
    useQuery: useModelRegistryQuery,
    invalidate: (queryClient) => queryClient.invalidateQueries({ queryKey: modelRegistryKey }),
  },
  prefs: {
    key: modelPrefsKey,
    fetch: fetchModelPrefs,
    useQuery: useModelPrefsQuery,
    invalidate: (queryClient) => queryClient.invalidateQueries({ queryKey: modelPrefsKey }),
  },
}

export const ownerQueries = {
  walkthrough: {
    key: walkthroughKey,
    fetch: fetchWalkthrough,
    useQuery: useWalkthroughQuery,
    invalidate: (queryClient) => queryClient.invalidateQueries({ queryKey: walkthroughKey }),
  },
}

// Convenience re-export used by ChatView's setQueryData calls.
export const chatMessagesQueryKey = chatQueries.keys.messages
