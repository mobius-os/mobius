import { useEffect, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { versionQueries } from '../../hooks/queries.js'
import useAgentRepair from '../../hooks/useAgentRepair.js'
import './ErrorBoundary.css'

// The served shell is a complete build, but the frontend source on disk has
// moved past it (an update, a merge, an edit the watcher never published).
// The owner would otherwise see "old" behaviour with no explanation. This is
// a quiet strip, not a blocking card: the shell works, it is just behind.
// While the watcher is rebuilding it says so and polls until the new
// generation lands (the ordinary shell-update refresh prompt takes over then).
const POLL_MS = 5000

const REPAIR_PROMPT = [
  'The Möbius shell being served is older than the frontend source in ' +
    '/data/platform/frontend, and the automatic rebuild did not publish a ' +
    'fresh bundle.',
  '',
  'Read the frontend watcher health and its last error (GET /api/debug/status ' +
    'and the server logs), find why the Vite build failed or was skipped, fix ' +
    'the cause in /data/platform, and make sure a fresh shell publishes. Do ' +
    'not reset or restore the platform; preserve every local edit.',
].join('\n')

export default function ShellFreshnessNotice({ version }) {
  const queryClient = useQueryClient()
  const [dismissedFor, setDismissedFor] = useState(null)
  const { repairActive, repair } = useAgentRepair({
    surfaceKey: 'shell-stale', prompt: REPAIR_PROMPT,
  })
  const served = version?.served_frontend || ''
  const stale = version?.frontend_source === 'platform' && version?.frontend_stale === true
  const building = version?.frontend_building === true
  const buildError = version?.frontend_build_error || null

  // Re-read the served identity while behind: a publish flips `stale` off
  // (and swaps `served_frontend`), which is the moment this strip can go.
  useEffect(() => {
    if (!stale) return undefined
    const timer = setInterval(() => {
      versionQueries.current.invalidate(queryClient)
    }, POLL_MS)
    return () => clearInterval(timer)
  }, [stale, queryClient])

  if (!stale || dismissedFor === served) return null

  let message
  if (building) {
    message = 'Rebuilding the interface with your latest changes…'
  } else if (buildError) {
    message = 'The interface couldn’t rebuild with your latest changes.'
  } else {
    message = 'The interface you’re seeing is behind your latest changes.'
  }

  return (
    <div className="shell-freshness" role="status" aria-live="polite">
      <span className="shell-freshness__text">{message}</span>
      {buildError && (
        <span className="shell-freshness__error" title={buildError}>
          {buildError.length > 120 ? `${buildError.slice(0, 117)}…` : buildError}
        </span>
      )}
      {!building && (
        <button
          type="button"
          className="shell-freshness__action"
          onClick={repair}
          disabled={repairActive}
        >
          {repairActive ? 'Opening…' : 'Ask the agent to fix it'}
        </button>
      )}
      <button
        type="button"
        className="shell-freshness__dismiss"
        aria-label="Hide this notice"
        onClick={() => setDismissedFor(served)}
      >
        ×
      </button>
    </div>
  )
}
