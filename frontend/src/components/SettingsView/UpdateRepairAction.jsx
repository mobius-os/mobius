/** Update-specific evidence uses the shared retry-safe agent repair lifecycle. */
import useAgentRepair from '../../hooks/useAgentRepair.js'
import useUpdateAgentChoice from './useUpdateAgentChoice.jsx'
import { buildPlatformUpdateRepairPrompt, platformUpdateRepairEvidence, platformUpdateRepairFingerprint } from '../../lib/platformUpdateRepair.js'

export default function UpdateRepairAction({ preview, platform, rebuild, error, errorCode, disabled, buttonRef, className = 'settings__btn settings__btn--sm', label = 'Ask Möbius' }) {
  const { chooseAgent, agentPicker } = useUpdateAgentChoice()
  const evidence = platformUpdateRepairEvidence({ preview, platform, rebuild, error, errorCode })
  const fingerprint = platformUpdateRepairFingerprint(evidence)
  const { repair, repairActive, attempt } = useAgentRepair({
    surfaceKey: 'platform-update', fingerprint,
    prompt: buildPlatformUpdateRepairPrompt(evidence),
  })
  const start = async () => {
    const agent = attempt?.agent || await chooseAgent()
    if (!agent) return
    await repair({ agent, archiveOnGoalComplete: true })
  }
  return <>
    <button ref={buttonRef} type="button" className={className} disabled={disabled || repairActive} onClick={start}>
      {repairActive ? 'Opening chat…' : label}
    </button>
    {attempt?.phase === 'agent-failed' && <p role="status" className="platform-updates__description">Couldn’t open the chat. Try again; the same request will be reused.</p>}
    {agentPicker}
  </>
}
