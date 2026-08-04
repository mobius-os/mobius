import { useEffect, useState } from 'react'
import { api, getToken } from '../../api/client.js'

export const RECOVERY_CONTROL_URL = 'https://www.mobius.you/'

function deploymentFromStatus(value) {
  return normalizeDeployment(value?.activation?.deployment)
}

function normalizeDeployment(deployment) {
  return deployment === 'railway' || deployment === 'self_hosted'
    ? deployment
    : null
}

/** Recovery is external, so nested errors must navigate the top-level control plane. */
export default function RecoveryLink({
  className = 'errbound__recovery',
  deployment = null,
  detectDeployment = true,
  lead = 'If the problem continues after trying again,',
}) {
  const suppliedDeployment = normalizeDeployment(deployment)
  const [detectedDeployment, setDetectedDeployment] = useState(null)
  const activeDeployment = suppliedDeployment || detectedDeployment

  useEffect(() => {
    if (!detectDeployment || suppliedDeployment || !getToken()) return undefined
    let cancelled = false
    api.platform.status()
      .then(async response => {
        if (!response.ok || cancelled) return
        const detected = deploymentFromStatus(await response.json())
        if (!cancelled && detected) setDetectedDeployment(detected)
      })
      .catch(() => {
        // Recovery guidance must remain usable when the platform-status read
        // is unavailable; the unresolved view keeps both external options.
      })
    return () => { cancelled = true }
  }, [detectDeployment, suppliedDeployment])

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
            Ask the server operator to run:
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
            Self-hosted: ask the server operator to run:
          </span>
          <code className="recovery-panel__recovery-command">mobiusctl recovery start</code>
        </>
      )}
    </p>
  )
}
