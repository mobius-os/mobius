/** Update-specific evidence uses the shared retry-safe agent repair lifecycle. */
import useAgentRepair from '../../hooks/useAgentRepair.js'
import { api } from '../../api/client.js'
import { errorRecoveryFingerprint } from '../../lib/errorRecovery.js'
import { buildPlatformUpdateRepairPrompt, platformUpdateRepairEvidence } from '../../lib/platformUpdateRepair.js'

export default function UpdateRepairAction({ preview, platform, rebuild, error, errorCode, disabled, buttonRef, className = 'settings__btn settings__btn--sm', label = 'Ask Möbius' }) {
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
  // Handing a reviewed update to an agent starts that update: Settings then
  // offers only Finish update for this release until it is done.
  const plan = preview?.operation === 'update' && preview?.plan_id
    ? { plan_id: preview.plan_id, current_sha: preview.current_sha, target_sha: preview.target_sha, image_digest: preview.image_digest }
    : null
  async function start() {
    if (plan) await api.platform.startUnfinishedUpdate(plan).catch(() => null)
    repair()
  }
  return <>
    <button ref={buttonRef} type="button" className={className} disabled={disabled || repairActive} onClick={start}>
      {repairActive ? 'Opening chat…' : label}
    </button>
    {attempt?.phase === 'agent-failed' && <p role="status" className="platform-updates__description">Couldn’t open the chat. Try again; the same request will be reused.</p>}
  </>
}
