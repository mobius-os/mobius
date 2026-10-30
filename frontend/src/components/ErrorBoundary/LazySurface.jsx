import { Component, Suspense, useLayoutEffect } from 'react'
import ErrorBoundary from './ErrorBoundary.jsx'
import { recordClientError } from '../../lib/errorLog.js'
import { reloadIfGenerationStale, safeServiceWorker } from '../../lib/shellUpdate.js'
import './LazySurface.css'

/** The loading dot a pane shows while its content is not ready yet. */
export function SurfaceLoading({ label = 'Loading' }) {
  return (
    <div className="surface-loading" role="status" aria-label={label}>
      <span className="surface-loading__dot" aria-hidden="true" />
    </div>
  )
}

/**
 * Counts visible surfaces whose code is still downloading (`hold`, wired to
 * LazySurface `onPending`) so the launch cover lifts only once the first
 * workspace paint was requested and none is pending. The last release waits
 * a frame for the pane to paint and re-checks then: an effect that releases
 * and re-acquires within that frame must keep the cover. `publish` must be
 * idempotent.
 */
export function createLaunchCoverHold(
  publish,
  scheduleFrame = callback => requestAnimationFrame(callback),
) {
  let pending = 0
  let requested = false
  const publishIfIdle = () => {
    if (requested && pending === 0) publish()
  }
  return {
    hold() {
      pending += 1
      return () => {
        pending -= 1
        if (pending === 0 && requested) scheduleFrame(publishIfIdle)
      }
    },
    request() {
      requested = true
      publishIfIdle()
    },
  }
}

function PendingSurface({ panel, onPending }) {
  useLayoutEffect(() => onPending?.(), [onPending])
  return panel
    ? <p className="lazy-panel-status" role="status">Opening…</p>
    : <SurfaceLoading />
}

// An on-request panel that fails to load must not take over the view it opens
// over (a chat that may still be streaming), so it shows a small notice the
// owner can close. Trying again needs a fresh page: Chromium keeps a failed
// module download for the life of the document, and Vite's preload helper does
// not request a chunk's stylesheet a second time.
class PanelLoadFailure extends Component {
  state = { failed: false }

  static getDerivedStateFromError() {
    return { failed: true }
  }

  componentDidCatch(error, info) {
    recordClientError({
      where: this.props.label,
      message: error?.message || error,
      error,
      componentStack: info?.componentStack,
    })
    // A deploy that replaced this build still reloads onto the newer one, as
    // the shared ErrorBoundary does; otherwise the notice stays until closed.
    reloadIfGenerationStale({
      serviceWorker: safeServiceWorker(),
      reload: () => window.location.reload(),
    }).catch(() => {})
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div className="lazy-panel-status" role="alert">
        <span>Couldn't open this panel.</span>
        <button type="button" className="lazy-panel-status__action" onClick={() => window.location.reload()}>
          Reload
        </button>
        <button type="button" className="lazy-panel-status__action" onClick={this.props.onDismiss}>
          Close
        </button>
      </div>
    )
  }
}

/**
 * A surface whose code downloads on first render (`React.lazy`).
 *
 * A pane shows the loading dot while its code downloads. A failed download or a
 * crash shows the shared inline ErrorBoundary in that pane, which reloads onto
 * a newer build when a deploy replaced this one.
 *
 * Passing onDismiss makes it a panel opened on request (an inspector, a dialog):
 * it shows a short "Opening…" line while loading, and a failure shows a small
 * notice offering Reload, or Close (onDismiss), instead of covering the view
 * below.
 *
 * Props:
 *   label, recoveryKey — name the surface for the crash record and recovery
 *                        ledger (see ErrorBoundary)
 *   onPending          — runs while the code downloads and may return a
 *                        cleanup, like an effect; Shell holds its launch cover
 *   onDismiss          — closes an on-request panel from its failure notice
 */
export default function LazySurface({
  label,
  recoveryKey,
  onPending,
  onDismiss,
  children,
}) {
  const panel = Boolean(onDismiss)
  const content = (
    <Suspense fallback={<PendingSurface panel={panel} onPending={onPending} />}>
      {children}
    </Suspense>
  )
  if (panel) {
    return <PanelLoadFailure label={label} onDismiss={onDismiss}>{content}</PanelLoadFailure>
  }
  return (
    <ErrorBoundary variant="inline" label={label} recoveryKey={recoveryKey || label}>
      {content}
    </ErrorBoundary>
  )
}
