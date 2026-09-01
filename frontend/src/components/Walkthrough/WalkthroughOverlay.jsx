import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Download } from '@openai/apps-sdk-ui/components/Icon'
import { api } from '../../api/client.js'
import { ownerQueries } from '../../hooks/queries.js'
import {
  getInstallPromptSnapshot,
  requestInstall,
  subscribeInstallPrompt,
} from '../../lib/installPrompt.js'
import { prepareShellInstallPass } from '../../lib/shellInstallPass.js'
import {
  detectInstallPlatform,
  installCopyForPlatform,
} from '../../utils/installPlatform.js'
import './WalkthroughOverlay.css'

export default function WalkthroughOverlay({ onDone, onOpenSettings, onExploreApps }) {
  const queryClient = useQueryClient()
  const closingRef = useRef(false)
  const installAbortRef = useRef(null)
  const [platform] = useState(() => detectInstallPlatform())
  const [installCopy] = useState(() => installCopyForPlatform(platform))
  const [showInstallHelp, setShowInstallHelp] = useState(false)
  const [installBusy, setInstallBusy] = useState(false)
  const [installFeedback, setInstallFeedback] = useState('')
  const installState = useSyncExternalStore(
    subscribeInstallPrompt,
    getInstallPromptSnapshot,
    getInstallPromptSnapshot,
  )

  function finish() {
    if (closingRef.current) return
    closingRef.current = true
    queryClient.setQueryData(ownerQueries.walkthrough.key, (prev) => ({
      ...(prev || { completed_at: null }),
      completed: true,
    }))
    try { localStorage.setItem('mobius:walkthrough-completed', '1') } catch (_) {}
    api.owner.walkthrough.complete().catch(() => {})
    onDone?.()
  }

  function takeAction(action) {
    finish()
    action?.()
  }

  async function handleInstall() {
    setInstallFeedback('')
    if (platform.ios) {
      const controller = new AbortController()
      installAbortRef.current = controller
      setInstallBusy(true)
      // Best effort: installing still works if the short sign-in handoff
      // cannot be refreshed; the new app will simply show normal login.
      await prepareShellInstallPass({ force: true, signal: controller.signal })
      if (controller.signal.aborted) return
      installAbortRef.current = null
      setInstallBusy(false)
    }
    if (installState !== 'ready') {
      setShowInstallHelp((visible) => !visible)
      return
    }

    setInstallBusy(true)
    const result = await requestInstall()
    setInstallBusy(false)
    if (result.outcome === 'accepted') {
      finish()
      return
    }

    setShowInstallHelp(true)
    setInstallFeedback(
      result.outcome === 'dismissed'
        ? 'Not installed. You can use the browser menu whenever you’re ready.'
        : 'The browser prompt wasn’t available. Use the steps below instead.',
    )
  }

  useEffect(() => {
    if (installState === 'installed') finish()
    // The event can arrive after mount; finish is guarded across rerenders.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [installState])

  useEffect(() => () => {
    installAbortRef.current?.abort()
  }, [])

  const nativeInstallReady = installState === 'ready'
  const installButtonLabel = installBusy
    ? 'Opening…'
    : (nativeInstallReady
        ? 'Install'
        : (showInstallHelp ? 'Hide' : installCopy.ctaLabel))

  return (
    <aside
      className="wt__card"
      role="region"
      aria-labelledby="wt-title"
    >
      <button
        type="button"
        className="wt__close"
        onClick={finish}
        aria-label="Dismiss welcome"
      >
        <span aria-hidden="true">×</span>
      </button>
      <div className="wt__mark" aria-hidden="true">
        <span />
      </div>
      <p className="wt__kicker">Your Möbius is ready</p>
      <h2 id="wt-title" className="wt__title">Start wherever you like.</h2>
      <p className="wt__body">
        You can explore now. Add an agent only when you want chats to act and
        build on your behalf.
      </p>
      <section className="wt__install" aria-labelledby="wt-install-title">
        <span className="wt__install-icon" aria-hidden="true">
          <Download width={18} height={18} />
        </span>
        <div className="wt__install-copy">
          <h3 id="wt-install-title">Keep Möbius close</h3>
          <p>
            {nativeInstallReady
              ? 'Install it on this device for a full-screen, one-tap launch.'
              : installCopy.summary}
          </p>
        </div>
        <button
          type="button"
          className="wt__install-btn"
          onClick={handleInstall}
          disabled={installBusy}
          aria-expanded={nativeInstallReady ? undefined : showInstallHelp}
          aria-controls={nativeInstallReady ? undefined : 'wt-install-help'}
        >
          {installButtonLabel}
        </button>
        {showInstallHelp && (
          <div className="wt__install-help" id="wt-install-help">
            <strong>{installCopy.title}</strong>
            <span>{installCopy.body}</span>
          </div>
        )}
        {installFeedback && (
          <p className="wt__install-feedback" role="status">
            {installFeedback}
          </p>
        )}
      </section>
      <div className="wt__paths">
        <button
          type="button"
          className="wt__path"
          onClick={() => takeAction(onOpenSettings)}
        >
          <span>Connect an agent</span>
          <small>Open Settings</small>
        </button>
        <button
          type="button"
          className="wt__path"
          onClick={() => takeAction(onExploreApps)}
        >
          <span>Find useful apps</span>
          <small>Open the App Store</small>
        </button>
      </div>
      <button type="button" className="wt__dismiss" onClick={finish}>
        I’ll explore
      </button>
    </aside>
  )
}
