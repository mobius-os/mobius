import { useCallback } from 'react'
import { requestChatQuestionReveal } from '../../lib/chatQuestionReveal.js'
import { settingsSectionOrNull } from '../../lib/settingsSections.js'

export function findAppForOpenTarget(list, target) {
  if (target == null) return null
  return (list || []).find(app =>
    String(app.id) === String(target) || app.slug === target) || null
}

export default function useAppIntentNavigation({
  appsRef,
  refreshApps,
  notifyShell,
  setAppIntents,
  navToRef,
  openSettingsSection,
}) {
  const openAppWithIntent = useCallback(async (
    target,
    rawIntent,
    shouldContinue = () => true,
    { paneId } = {},
  ) => {
    let app = findAppForOpenTarget(appsRef.current, target)
    if (!app) {
      const updatedApps = await refreshApps()
      app = findAppForOpenTarget(updatedApps, target)
    }
    if (!shouldContinue()) return
    if (!app) {
      notifyShell('App is not installed yet.', {
        variant: 'info',
      })
      return
    }
    const intent = typeof rawIntent === 'string' ? rawIntent.trim() : ''
    if (intent) {
      setAppIntents((prev) => ({
        ...prev,
        [String(app.id)]: { intent, nonce: Date.now() },
      }))
    }
    navToRef.current('canvas', {
      appId: app.id,
      ...(typeof paneId === 'string' && paneId ? { paneId } : {}),
    })
  }, [refreshApps, notifyShell])

  const handleChatInternalNav = useCallback((url) => {
    const app = url.searchParams.get('app')
    const chat = url.searchParams.get('chat')
    const intent = url.searchParams.get('intent')
    if (url.searchParams.has('settings')) {
      openSettingsSection(settingsSectionOrNull(url.searchParams.get('settings')))
    } else if (app) {
      void openAppWithIntent(app, intent)
    } else if (chat) {
      if (url.searchParams.get('focus') === 'question') {
        requestChatQuestionReveal(chat, url.searchParams.get('question'))
      }
      navToRef.current('chat', { chatId: chat })
    }
  }, [openAppWithIntent, openSettingsSection])

  return { openAppWithIntent, handleChatInternalNav }
}
