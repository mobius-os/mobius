import { useState } from 'react'
import { api, BASE } from '../../api/client.js'
import {
  errorRecoveryFingerprint,
  readErrorRecoveryAttempt,
  runAgentRepair,
  writeRefreshedRecoveryAttempt,
} from '../../lib/errorRecovery.js'
import RecoveryPanel from './RecoveryPanel.jsx'
import './ErrorBoundary.css'

// The backend reports `platform_degraded` when the entrypoint is serving the
// image-baked floor because /data/platform failed to import. The app still
// works (it is the built-in copy), so this is not a crash — but it must not run
// silently. We surface the SAME refresh -> ask-agent recovery flow the error
// boundary uses, driven by the shared errorRecovery ledger, rather than a
// parallel popup. A fixed surface key gives the refresh->agent escalation a
// stable identity across reloads without a component stack to fingerprint.
const SURFACE_KEY = 'platform-degraded'
const FINGERPRINT = errorRecoveryFingerprint(SURFACE_KEY, 'platform-degraded', '')
const DIAGNOSTIC =
  'Möbius is serving the built-in version because your latest changes to ' +
  '/data/platform did not load. Your edits are preserved on disk but are not ' +
  'running.'

// Not a UI crash, so buildAgentRepairPrompt (which frames a React failure with a
// component stack) does not fit. Tell the agent the real situation instead.
const REPAIR_PROMPT = [
  'Möbius is running the built-in fallback because /data/platform failed to ' +
    'import at boot, so the latest edits are on disk but are not being served.',
  '',
  'Reproduce the import failure from the served backend, read the relevant ' +
    'boot/container logs, find the root cause in /data/platform, and implement ' +
    'a targeted fix so the normal platform serves again.',
  '',
  'Preserve the edits and all data. Do not reset or restore the platform unless ' +
    'ordinary diagnosis and a targeted fix cannot make progress. The app must be ' +
    'restarted to pick up the repaired tree.',
].join('\n')

export default function PlatformDegradedNotice({ onContinue }) {
  const [attempt, setAttempt] = useState(() =>
    readErrorRecoveryAttempt({ surfaceKey: SURFACE_KEY, fingerprint: FINGERPRINT }),
  )
  const [repairActive, setRepairActive] = useState(false)

  const handleRefresh = () => {
    // Record the refresh so a still-degraded reload escalates to "ask agent",
    // exactly like the error boundary's manual refresh.
    writeRefreshedRecoveryAttempt({ surfaceKey: SURFACE_KEY, fingerprint: FINGERPRINT })
    try { sessionStorage.setItem('shell-reload', '1') } catch { /* ignore */ }
    window.location.reload()
  }

  const handleAgentRepair = async () => {
    if (repairActive) return
    setRepairActive(true)
    try {
      const result = await runAgentRepair({
        client: api,
        base: BASE,
        surfaceKey: SURFACE_KEY,
        fingerprint: FINGERPRINT,
        previousAttempt: attempt,
        onAttempt: setAttempt,
        prompt: REPAIR_PROMPT,
      })
      window.location.assign(result.path)
    } catch (error) {
      if (error?.name === 'AbortError') return
    } finally {
      setRepairActive(false)
    }
  }

  return (
    <div className="errbound">
      <div className="platform-degraded">
        <RecoveryPanel
          variant="boundary"
          className="errbound__card"
          title="Your latest changes didn’t load"
          subject="app"
          diagnostic={DIAGNOSTIC}
          attempt={attempt}
          repairActive={repairActive}
          refreshLabel="Refresh"
          onRefresh={handleRefresh}
          onAgentRepair={handleAgentRepair}
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
