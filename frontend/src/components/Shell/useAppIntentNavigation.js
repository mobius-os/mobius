import { useCallback } from 'react'

// Legacy slug aliases keep old deep links working after an app is renamed.
// The Pages app was historically slugged "artifacts".
const LEGACY_SLUG_ALIASES = { artifacts: 'pages' }

export function findAppForOpenTarget(list, target) {
  if (target == null) return null
  const resolved = LEGACY_SLUG_ALIASES[target] || target
  return (list || []).find(app =>
    String(app.id) === String(target) || app.slug === resolved) || null
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
    { paneId } = {},
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
    navToRef.current('canvas', {
      appId: app.id,
      ...(typeof paneId === 'string' && paneId ? { paneId } : {}),
    })
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
