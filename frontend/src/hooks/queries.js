import { useQuery } from '@tanstack/react-query'
import { api } from '../api/client.js'

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
  })
}

async function fetchClaudeProviderStatus() {
  const res = await api.auth.provider.claude.status()
  return jsonOrThrow(res, 'provider status fetch failed:')
}

function useClaudeProviderStatusQuery() {
  return useQuery({
    queryKey: providerClaudeStatusKey,
    queryFn: fetchClaudeProviderStatus,
  })
}

async function fetchProvidersStatus() {
  const res = await api.auth.provider.statuses()
  return jsonOrNull(res)
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

export const themeQueries = {
  keys: {
    all: themeKey,
    mode: themeModeKey,
  },
  fetch: fetchTheme,
  useQuery: useThemeQuery,
  invalidate: (queryClient) => queryClient.invalidateQueries({ queryKey: themeKey }),
  mode: {
    key: themeModeKey,
    fetch: fetchThemeMode,
    useQuery: useThemeModeQuery,
    invalidate: (queryClient) => queryClient.invalidateQueries({ queryKey: themeModeKey }),
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

export const themeQueryKey = themeQueries.keys.all
export const chatsQueryKey = chatQueries.keys.all
export const chatMessagesQueryKey = chatQueries.keys.messages
export const appsQueryKey = appQueries.keys.all
export const appTokenQueryKey = appQueries.keys.token
