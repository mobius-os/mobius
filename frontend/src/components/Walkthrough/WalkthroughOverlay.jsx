import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Download } from 'lucide-react'
import { api } from '../../api/client.js'
import { ownerQueries } from '../../hooks/queries.js'
import {
  getInstallPromptSnapshot,
  requestInstall,
  subscribeInstallPrompt,
} from '../../lib/installPrompt.js'
import {
  detectInstallPlatform,
  installCopyForPlatform,
} from '../../utils/installPlatform.js'
import './WalkthroughOverlay.css'

export default function WalkthroughOverlay({ onDone, onOpenSettings, onExploreApps }) {
  const queryClient = useQueryClient()
  const closingRef = useRef(false)
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

  const nativeInstallReady = installState === 'ready'
  const installButtonLabel = installBusy
    ? 'Opening…'
    : (nativeInstallReady
        ? 'Install Möbius'
        : (showInstallHelp ? 'Hide install help' : 'How to install'))

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
      <h2 id="wt-title" className="wt__title">Your Möbius is ready.</h2>
      <p className="wt__body">
        Explore now. Connect an agent when you want Möbius to build with you.
      </p>
      <div className="wt__paths">
        <button
          type="button"
          className="wt__path"
          onClick={() => takeAction(onOpenSettings)}
        >
          Connect agent
        </button>
        <button
          type="button"
          className="wt__path"
          onClick={() => takeAction(onExploreApps)}
        >
          Explore apps
        </button>
      </div>
      <div className="wt__install">
        <button
          type="button"
          className="wt__install-btn"
          onClick={handleInstall}
          disabled={installBusy}
          aria-expanded={nativeInstallReady ? undefined : showInstallHelp}
          aria-controls={nativeInstallReady ? undefined : 'wt-install-help'}
        >
          <Download size={15} strokeWidth={2} aria-hidden="true" />
          {installButtonLabel}
        </button>
        {showInstallHelp && (
          <p className="wt__install-help" id="wt-install-help">
            <strong>{installCopy.title}.</strong> {installCopy.body}
          </p>
        )}
        {installFeedback && (
          <p className="wt__install-feedback" role="status">
            {installFeedback}
          </p>
        )}
      </div>
    </aside>
  )
}
