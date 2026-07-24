import { useEffect, useRef } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { ownerQueries } from '../../hooks/queries.js'
import './WalkthroughOverlay.css'

export default function WalkthroughOverlay({ onDone, onOpenSettings, onExploreApps }) {
  const queryClient = useQueryClient()
  const closingRef = useRef(false)

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

  useEffect(() => {
    const standalone =
      (typeof window !== 'undefined' &&
        window.matchMedia &&
        window.matchMedia('(display-mode: standalone)').matches) ||
      (typeof navigator !== 'undefined' && navigator.standalone === true)
    if (standalone) finish()
    // finish is guarded and this should run only once on mount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
