/* "Keep Möbius close": installs the shell as an app on this device. */
import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { Download } from '@openai/apps-sdk-ui/components/Icon'
import { getInstallPromptSnapshot, requestInstall, subscribeInstallPrompt } from '../../lib/installPrompt.js'
import { prepareShellInstallPass } from '../../lib/shellInstallPass.js'
import { detectInstallPlatform, installCopyForPlatform } from '../../utils/installPlatform.js'

export default function WalkthroughInstall() {
  const installAbortRef = useRef(null)
  const [platform] = useState(() => detectInstallPlatform())
  const [installCopy] = useState(() => installCopyForPlatform(platform))
  const [showHelp, setShowHelp] = useState(false)
  const [busy, setBusy] = useState(false)
  const [feedback, setFeedback] = useState('')
  const installState = useSyncExternalStore(subscribeInstallPrompt, getInstallPromptSnapshot, getInstallPromptSnapshot)

  useEffect(() => () => installAbortRef.current?.abort(), [])

  async function handleInstall() {
    setFeedback('')
    if (platform.ios) {
      const controller = new AbortController()
      installAbortRef.current = controller
      setBusy(true)
      await prepareShellInstallPass({ force: true, signal: controller.signal })
      if (controller.signal.aborted) return
      installAbortRef.current = null
      setBusy(false)
    }
    if (installState !== 'ready') {
      setShowHelp(value => !value)
      return
    }
    setBusy(true)
    const result = await requestInstall()
    setBusy(false)
    if (result.outcome === 'accepted') {
      setFeedback('Installed on this device. Your guide is still here.')
      return
    }
    if (result.outcome === 'fallback-ready') {
      setFeedback('Tap Install again to use your browser’s regular prompt.')
      return
    }
    setShowHelp(true)
    setFeedback(result.outcome === 'dismissed'
      ? 'Not installed. You can do this from your browser menu later.'
      : 'The browser prompt was unavailable. Use the steps below instead.')
  }

  const label = busy ? 'Opening…' : installState === 'ready' ? 'Install' : showHelp ? 'Hide' : installCopy.ctaLabel
  if (installState === 'installed') {
    return <section className="wt-install is-installed" aria-labelledby="wt-install-title">
      <span className="wt-install__icon" aria-hidden="true"><Download width={20} height={20} /></span>
      <div><h3 id="wt-install-title">Möbius is on this device</h3><p>Open it from your home screen or app launcher any time.</p></div>
    </section>
  }
  return <section className="wt-install" aria-labelledby="wt-install-title">
    <span className="wt-install__icon" aria-hidden="true"><Download width={20} height={20} /></span>
    <div><h3 id="wt-install-title">Keep Möbius close</h3><p>{installState === 'ready' ? 'Install it for a full-screen, one-tap launch from your home screen.' : installCopy.summary}</p></div>
    <button type="button" className="wt-btn wt-btn--primary" onClick={handleInstall} disabled={busy} aria-expanded={installState === 'ready' ? undefined : showHelp} aria-controls={installState === 'ready' ? undefined : 'wt-install-help'}>{label}</button>
    {showHelp && <div className="wt-install__help" id="wt-install-help"><strong>{installCopy.title}</strong><span>{installCopy.body}</span></div>}
    {feedback && <p className="wt-install__feedback" role="status">{feedback}</p>}
  </section>
}
