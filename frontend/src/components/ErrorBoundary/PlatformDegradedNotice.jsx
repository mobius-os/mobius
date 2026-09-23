import useAgentRepair from '../../hooks/useAgentRepair.js'
import { BASE } from '../../api/client.js'
import { repairChatPath } from '../../lib/errorRecovery.js'
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
// offer one direct recovery choice rather than making the owner refresh a state
// that boot has already proven cannot load. A fixed surface key gives retries a
// stable identity across reloads without a component stack to fingerprint.
const VARIANTS = {
  backend: {
    surfaceKey: 'platform-degraded',
    title: 'Your latest changes didn’t load',
    body:
      'Möbius opened its working built-in version. ' +
      'An agent can fix the latest changes, or you can keep using this version.',
    diagnostic:
      'Möbius is serving its protected fallback because the latest platform ' +
      'changes did not finish loading. The latest source is preserved but ' +
      'is not running.',
    // Not a UI crash, so buildAgentRepairPrompt (which frames a React failure
    // with a component stack) does not fit. Tell the agent the real situation.
    repairPrompt: [
      'Möbius is running its protected fallback because the latest platform ' +
        'changes did not finish loading. The latest source is preserved but ' +
        'is not being served.',
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
    title: 'Your latest interface didn’t load',
    body:
      'Möbius opened its working built-in interface. ' +
      'An agent can fix the latest changes, or you can keep using this interface.',
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
  const { attempt, repairActive, repair, error } = useAgentRepair({
    surfaceKey: copy.surfaceKey, prompt: copy.repairPrompt,
  })
  const repairChatId = attempt?.chatId || null
  const repairDirected = attempt?.phase === 'agent-directed' && repairChatId
  const repairFailed = attempt?.phase === 'agent-failed'
  const handleRepair = () => {
    if (repairDirected) {
      window.location.assign(repairChatPath(repairChatId, BASE))
      return
    }
    void repair()
  }

  return (
    <div className="errbound">
      <div className="platform-degraded">
        <section className="recovery-panel recovery-panel--boundary errbound__card">
          <h1 className="recovery-panel__title">{copy.title}</h1>
          <p className="recovery-panel__body">{copy.body}</p>
          <details className="recovery-panel__details">
            <summary>Technical details</summary>
            <pre className="recovery-panel__detail">{copy.diagnostic}</pre>
          </details>
          {(repairActive || repairFailed || error) && (
            <p className="recovery-panel__status" role="status" aria-live="polite">
              {repairActive
                ? 'Opening the repair chat…'
                : error || 'The repair request did not go through. You can try again.'}
            </p>
          )}
          <div className="platform-degraded__actions">
            <button
              type="button"
              className="recovery-panel__button recovery-panel__button--primary platform-degraded__action"
              onClick={handleRepair}
              disabled={repairActive}
            >
              <span>{repairActive
                ? 'Opening repair chat…'
                : repairDirected
                  ? 'Open repair chat'
                  : repairFailed
                    ? 'Try agent fix again'
                    : 'Fix with an agent'}</span>
              {!repairActive && !repairDirected && !repairFailed && (
                <small>Recommended</small>
              )}
            </button>
            {onContinue && (
              <button
                type="button"
                className="recovery-panel__button platform-degraded__action"
                onClick={onContinue}
              >
                Keep using this version
              </button>
            )}
          </div>
        </section>
      </div>
    </div>
  )
}
