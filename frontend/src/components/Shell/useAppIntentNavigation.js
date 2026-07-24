import { useCallback } from 'react'

export function findAppForOpenTarget(list, target) {
  if (target == null) return null
  return (list || []).find(app =>
    String(app.id) === String(target) || app.slug === target) || null
}

export default function useAppIntentNavigation({
  appsRef,
  refreshApps,
  showToast,
  setAppIntents,
  navToRef,
}) {
  const openAppWithIntent = useCallback(async (
    target,
    rawIntent,
    shouldContinue = () => true,
  ) => {
    let app = findAppForOpenTarget(appsRef.current, target)
    if (!app) {
      const updatedApps = await refreshApps()
      app = findAppForOpenTarget(updatedApps, target)
    }
    if (!shouldContinue()) return
    if (!app) {
      showToast('App is not installed yet.', {
        variant: 'info',
        duration: 6000,
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
    navToRef.current('canvas', { appId: app.id })
  }, [refreshApps, showToast])

  const handleChatInternalNav = useCallback((url) => {
    const app = url.searchParams.get('app')
    const chat = url.searchParams.get('chat')
    const intent = url.searchParams.get('intent')
    if (app) {
      void openAppWithIntent(app, intent)
    } else if (chat) {
      navToRef.current('chat', { chatId: chat })
    }
  }, [openAppWithIntent])

  return { openAppWithIntent, handleChatInternalNav }
}
