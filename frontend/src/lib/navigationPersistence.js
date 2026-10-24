import { isSharedBrowserRoute } from './sharedBrowserWorkspace.js'
import { settingsSectionOrNull } from './settingsSections.js'
import { localStore, sessionStore } from './workspaceStorage.js'

const ACTIVE_CHAT_KEY = 'moebius_active_chat'
const ACTIVE_VIEW_KEY = 'moebius_active_view'
const ACTIVE_APP_KEY = 'moebius_active_app'
const RETURN_VIEW_KEY = 'mobius:return-view'

export function readStoredChatId(storage = localStore()) {
  try { return storage?.getItem(ACTIVE_CHAT_KEY) ?? null } catch { return null }
}

export function readRestoredCanvas(storage = localStore()) {
  try {
    const view = storage?.getItem(ACTIVE_VIEW_KEY)
    const app = storage?.getItem(ACTIVE_APP_KEY)
    if (view !== 'canvas' || !app) return null
    const appId = Number.parseInt(app, 10)
    return Number.isFinite(appId) ? { view: 'canvas', appId } : null
  } catch { return null }
}

export function consumeReturnView(storage = sessionStore()) {
  try {
    const view = storage?.getItem(RETURN_VIEW_KEY)
    storage?.removeItem(RETURN_VIEW_KEY)
    return view === 'settings' ? { view: 'settings' } : null
  } catch { return null }
}

export function parseShellDeepLink(location = globalThis.location) {
  const path = location?.pathname || ''
  if (/^\/shell\/?$/.test(path) || isSharedBrowserRoute(path)) {
    try {
      const params = new URLSearchParams(location?.search || '')
      const app = params.get('app')
      const chat = params.get('chat')
      const project = params.get('project')
      const projects = params.get('projects')
      const intent = params.get('intent')
      const focusQuestion = params.get('focus') === 'question'
      if (app) {
        const appId = /^\d+$/.test(app) ? Number.parseInt(app, 10) : null
        return { view: 'canvas', app, appId, intent }
      }
      if (chat) return { view: 'chat', chatId: chat, intent, focusQuestion,
        ...(focusQuestion && params.get('question') ? { questionId: params.get('question') } : {}) }
      if (project) return { view: 'project', projectId: project }
      if (projects === '1') return { view: 'projects' }
      if (params.has('settings')) {
        const section = settingsSectionOrNull(params.get('settings'))
        return { view: 'settings', ...(section ? { section } : {}) }
      }
    } catch { /* malformed query is an ordinary empty destination */ }
    return null
  }
  // Legacy path-form deep links still arrive on a fresh page load (the
  // screenshot helper's /app/<id>, the AI-provider OAuth return to /settings).
  // Return the same shapes as the /shell/?app= and ?chat= forms above so the
  // ordinary boot and slug resolution run.
  const appMatch = path.match(/^\/app\/([^/]+)\/?$/)
  if (appMatch) {
    const app = appMatch[1]
    const appId = /^\d+$/.test(app) ? Number.parseInt(app, 10) : null
    return { view: 'canvas', app, appId, intent: null }
  }
  const chatMatch = path.match(/^\/chat\/([^/]+)\/?$/)
  if (chatMatch) return { view: 'chat', chatId: chatMatch[1], intent: null, focusQuestion: false }
  if (/^\/settings\/?$/.test(path)) {
    // The OAuth callback needs this one section after a cold navigation. Do
    // not carry arbitrary URL input into SettingsView's focus target.
    const params = new URLSearchParams(location?.search || '')
    const sections = params.getAll('section')
    if (sections.length !== 1 || sections[0] !== 'ai-providers') return { view: 'settings' }
    const returned = ['mobius_enroll_return', 'mobius_enroll_error'].some(key => {
      const values = params.getAll(key)
      return values.length === 1 && values[0] === '1'
    })
    return { view: 'settings', section: 'ai-providers', ...(returned ? { providerReturn: true } : {}) }
  }
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
