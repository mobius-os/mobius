const ACTIVE_CHAT_KEY = 'moebius_active_chat'
const ACTIVE_VIEW_KEY = 'moebius_active_view'
const ACTIVE_APP_KEY = 'moebius_active_app'
const RETURN_VIEW_KEY = 'mobius:return-view'

export function readStoredChatId(storage = globalThis.localStorage) {
  try { return storage?.getItem(ACTIVE_CHAT_KEY) ?? null } catch { return null }
}

export function readRestoredCanvas(storage = globalThis.localStorage) {
  try {
    const view = storage?.getItem(ACTIVE_VIEW_KEY)
    const app = storage?.getItem(ACTIVE_APP_KEY)
    if (view !== 'canvas' || !app) return null
    const appId = Number.parseInt(app, 10)
    return Number.isFinite(appId) ? { view: 'canvas', appId } : null
  } catch { return null }
}

export function consumeReturnView(storage = globalThis.sessionStorage) {
  try {
    const view = storage?.getItem(RETURN_VIEW_KEY)
    storage?.removeItem(RETURN_VIEW_KEY)
    return view === 'settings' ? { view: 'settings' } : null
  } catch { return null }
}

export function parseShellDeepLink(location = globalThis.location) {
  const path = location?.pathname || ''
  if (/^\/shell\/?$/.test(path)) {
    try {
      const params = new URLSearchParams(location?.search || '')
      const app = params.get('app')
      const chat = params.get('chat')
      const project = params.get('project')
      const projects = params.get('projects')
      const intent = params.get('intent')
      if (app) {
        const appId = /^\d+$/.test(app) ? Number.parseInt(app, 10) : null
        return { view: 'canvas', app, appId, intent }
      }
      if (chat) return { view: 'chat', chatId: chat, intent }
      if (project) return { view: 'project', projectId: project }
      if (projects === '1') return { view: 'projects' }
    } catch { /* malformed query is an ordinary empty destination */ }
    return null
  }
  const appMatch = path.match(/^\/app\/([^/]+)$/)
  const chatMatch = path.match(/^\/chat\/([^/]+)$/)
  if (appMatch) return { view: 'canvas', appId: Number.parseInt(appMatch[1], 10) }
  if (chatMatch) return { view: 'chat', chatId: chatMatch[1] }
  return null
}

export function persistActiveNavigation(
  storage, { activeView, activeChatId, activeAppId },
) {
  try {
    if (activeChatId) storage?.setItem(ACTIVE_CHAT_KEY, activeChatId)
    storage?.setItem(ACTIVE_VIEW_KEY, activeView)
    if (activeView === 'canvas' && activeAppId != null) {
      storage?.setItem(ACTIVE_APP_KEY, String(activeAppId))
    } else if (activeView !== 'canvas') {
      storage?.removeItem(ACTIVE_APP_KEY)
    }
  } catch { /* private mode / disabled storage: navigation remains live */ }
}
