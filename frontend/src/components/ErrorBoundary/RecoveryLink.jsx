import { useEffect, useState } from 'react'
import { api, getToken } from '../../api/client.js'
import { deploymentKind } from '../../lib/platformUpdateState.js'

export const RECOVERY_CONTROL_URL = 'https://www.mobius.you/'

/** Recovery is external, so nested errors must navigate the top-level control plane. */
export default function RecoveryLink({
  className = 'errbound__recovery',
  lead = 'If the problem continues after trying again,',
}) {
  // Every render site is a fallback for a surface that has already failed —
  // a crashed boundary, a crashed app host, or a startup error — so there is
  // no healthy app state to inherit a deployment from. This link owns the one
  // read it needs. `getToken()` is the gate: the status route is owner-only,
  // so without credentials the read cannot succeed and is not attempted.
  const [activeDeployment, setActiveDeployment] = useState(null)

  useEffect(() => {
    if (!getToken()) return undefined
    let cancelled = false
    api.platform.status()
      .then(async response => {
        if (!response.ok || cancelled) return
        const detected = deploymentKind((await response.json())?.activation)
        if (!cancelled && detected) setActiveDeployment(detected)
      })
      .catch(() => {
        // Recovery guidance must remain usable when the platform-status read
        // is unavailable; the unresolved view keeps both external options.
      })
    return () => { cancelled = true }
  }, [])

  return (
    <p className={className}>
      <span className="recovery-panel__recovery-lead">{lead}</span>
      {activeDeployment === 'railway' && (
        <>
          <span className="recovery-panel__recovery-context">This instance is managed on Railway.</span>
          <span className="recovery-panel__recovery-action">
            <a href={RECOVERY_CONTROL_URL} target="_top">Open Recovery in mobius.you</a>.
          </span>
        </>
      )}
      {activeDeployment === 'self_hosted' && (
        <>
          <span className="recovery-panel__recovery-context">This is a self-hosted Möbius instance.</span>
          <span className="recovery-panel__recovery-action">
            Run this on the server:
          </span>
          <code className="recovery-panel__recovery-command">mobiusctl recovery start</code>
        </>
      )}
      {!activeDeployment && (
        <>
          <span className="recovery-panel__recovery-action">
            Managed hosting: <a href={RECOVERY_CONTROL_URL} target="_top">open Recovery in mobius.you</a>.
          </span>
          <span className="recovery-panel__recovery-action">
            Self-hosted — run this on the server:
          </span>
          <code className="recovery-panel__recovery-command">mobiusctl recovery start</code>
        </>
      )}
    </p>
  )
}
