/** Update-specific evidence uses the shared retry-safe agent repair lifecycle. */
import useAgentRepair from '../../hooks/useAgentRepair.js'
import { errorRecoveryFingerprint } from '../../lib/errorRecovery.js'
import { buildPlatformUpdateRepairPrompt, platformUpdateRepairEvidence } from '../../lib/platformUpdateRepair.js'

export default function UpdateRepairAction({ preview, platform, rebuild, error, errorCode, disabled, buttonRef, className = 'settings__btn settings__btn--sm' }) {
  const evidence = platformUpdateRepairEvidence({ preview, platform, rebuild, error, errorCode })
  const fingerprint = errorRecoveryFingerprint('platform-update', JSON.stringify({
    release: evidence.reviewed_release, installed: evidence.installed_release,
    paths: evidence.blocking_paths, actions: evidence.activation?.required_actions,
    code: evidence.error_code, error: evidence.error,
  }))
  const { repair, repairActive, attempt } = useAgentRepair({
    surfaceKey: 'platform-update', fingerprint,
    prompt: buildPlatformUpdateRepairPrompt(evidence),
  })
  return <>
    <button ref={buttonRef} type="button" className={className} disabled={disabled || repairActive} onClick={repair}>
      {repairActive ? 'Opening chat…' : 'Ask Möbius'}
    </button>
    {attempt?.phase === 'agent-failed' && <p role="status" className="platform-updates__description">Couldn’t open the chat. Try again; the same request will be reused.</p>}
  </>
}
