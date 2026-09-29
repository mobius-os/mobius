import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from '../../api/client.js'
import AppIcon from '../AppIcon.jsx'
import {
  appUpdateNoticeIdentity,
  dismissAppUpdate,
  isAppUpdateDismissed,
} from './appUpdateNotice.js'

export default function AppUpdateNotice({
  appId,
  appName,
  app,
  active,
  appStoreAvailable,
  onOpenAppStore,
}) {
  const [notice, setNotice] = useState(null)
  const sessionDismissed = useRef(new Set())

  useEffect(() => {
    setNotice(null)
    if (!active || appId == null) return undefined
    const controller = new AbortController()
    let current = true

    const checkForUpdate = async () => {
      try {
        const response = await api.apps.updateCheck(appId, { signal: controller.signal })
        if (!response.ok) return
        const result = await response.json()
        if (!current) return
        const digest = appUpdateNoticeIdentity(result)
        const sessionKey = `${appId}:${digest || ''}`
        if (
          !digest ||
          isAppUpdateDismissed(appId, digest) ||
          sessionDismissed.current.has(sessionKey)
        ) return
        setNotice(digest)
      } catch {
        // Offline or unsupported apps stay usable; the next app entry can check again.
      }
    }

    void checkForUpdate()
    return () => {
      current = false
      controller.abort()
    }
  }, [active, appId])

  const dismiss = useCallback(() => {
    if (!notice) return
    sessionDismissed.current.add(`${appId}:${notice}`)
    dismissAppUpdate(appId, notice)
    setNotice(null)
    return true
  }, [appId, notice])

  const openStore = useCallback(() => {
    if (!dismiss()) return
    onOpenAppStore?.()
  }, [dismiss, onOpenAppStore])

  if (!active || !notice) return null

  return (
    <section
      className="app-update-notice"
      role="region"
      aria-label="App update available"
    >
      <AppIcon
        item={app}
        label={appName || 'App'}
        className="app-update-notice__app-icon"
      />
      <div className="app-update-notice__copy">
        <p>{appName || 'This app'} has an update</p>
        <span className="app-update-notice__meta">Ready to review in the App Store</span>
      </div>
      <div className="app-update-notice__actions">
        {appStoreAvailable && (
          <button
            className="app-update-notice__primary"
            type="button"
            onClick={openStore}
            aria-label="Review update in App Store"
          >
            <span className="app-update-notice__primary-label--wide">Review update</span>
            <span className="app-update-notice__primary-label--compact" aria-hidden="true">Review</span>
          </button>
        )}
        <button
          className="app-update-notice__dismiss"
          type="button"
          aria-label="Dismiss update notice"
          title="Dismiss update notice"
          onClick={dismiss}
        >
          <span className="app-update-notice__dismiss-icon" aria-hidden="true">×</span>
        </button>
      </div>
    </section>
  )
}
