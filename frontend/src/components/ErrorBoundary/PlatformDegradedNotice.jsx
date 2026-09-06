import useAgentRepair from '../../hooks/useAgentRepair.js'
import RecoveryPanel from './RecoveryPanel.jsx'
import './ErrorBoundary.css'

// `/api/version` reports which tree is ACTUALLY serving. Two fallbacks exist,
// and both must not run silently because the owner's latest edits are on disk
// but are not what they are looking at:
//
// - `serving_source: "baked"` — the entrypoint serves the image-baked BACKEND
//   because /data/platform failed to import at boot.
// - `frontend_source: "baked"` — the platform backend serves the image-baked
//   SHELL because /data/platform/frontend/dist is not a complete build (the
//   watcher never published one, or the last publish was rejected).
//
// The app still works (it is the built-in copy), so this is not a crash. We
// surface the SAME refresh -> ask-agent recovery flow the error boundary uses,
// driven by the shared errorRecovery ledger, rather than a parallel popup. A
// fixed surface key gives the refresh->agent escalation a stable identity
// across reloads without a component stack to fingerprint.
const VARIANTS = {
  backend: {
    surfaceKey: 'platform-degraded',
    title: 'Your latest changes didn’t load',
    diagnostic:
      'Möbius is serving the built-in version because your latest changes to ' +
      '/data/platform did not load. Your edits are preserved on disk but are ' +
      'not running.',
    // Not a UI crash, so buildAgentRepairPrompt (which frames a React failure
    // with a component stack) does not fit. Tell the agent the real situation.
    repairPrompt: [
      'Möbius is running the built-in fallback because /data/platform failed ' +
        'to import at boot, so the latest edits are on disk but are not being ' +
        'served.',
      '',
      'Reproduce the import failure from the served backend, read the relevant ' +
        'boot/container logs, find the root cause in /data/platform, and ' +
        'implement a targeted fix so the normal platform serves again.',
      '',
      'Preserve the edits and all data. Do not reset or restore the platform ' +
        'unless ordinary diagnosis and a targeted fix cannot make progress. The ' +
        'app must be restarted to pick up the repaired tree.',
    ].join('\n'),
  },
  frontend: {
    surfaceKey: 'shell-degraded',
    title: 'You’re seeing the built-in interface',
    diagnostic:
      'Möbius is showing its built-in interface because the edited interface ' +
      'in /data/platform did not build. Your edits are preserved on disk but ' +
      'are not what you are looking at.',
    repairPrompt: [
      'Möbius is serving the image-baked shell because ' +
        '/data/platform/frontend/dist is not a complete build, so the latest ' +
        'frontend edits are on disk but are not being shown.',
      '',
      'Read the frontend watcher health and last error (GET /api/debug/status ' +
        'and the server logs), find why the Vite build failed or was never ' +
        'published, fix the cause in /data/platform, and make sure a complete ' +
        'shell publishes to /data/platform/frontend/dist.',
      '',
      'Preserve the edits and all data. Do not reset or restore the platform ' +
        'unless ordinary diagnosis and a targeted fix cannot make progress.',
    ].join('\n'),
  },
}

export default function PlatformDegradedNotice({ onContinue, variant = 'backend' }) {
  const copy = VARIANTS[variant] || VARIANTS.backend
  const { attempt, repairActive, repair, markRefreshed } = useAgentRepair({
    surfaceKey: copy.surfaceKey, prompt: copy.repairPrompt,
  })

  const handleRefresh = () => {
    markRefreshed()
    window.location.reload()
  }

  return (
    <div className="errbound">
      <div className="platform-degraded">
        <RecoveryPanel
          variant="boundary"
          className="errbound__card"
          title={copy.title}
          subject="app"
          diagnostic={copy.diagnostic}
          attempt={attempt}
          repairActive={repairActive}
          refreshLabel="Refresh"
          onRefresh={handleRefresh}
          onAgentRepair={repair}
        />
        {onContinue && (
          <button
            type="button"
            className="platform-degraded__continue"
            onClick={onContinue}
          >
            Continue to the built-in version
          </button>
        )}
      </div>
    </div>
  )
}
