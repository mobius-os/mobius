export const RECOVERY_CONTROL_URL = 'https://www.mobius.you/'

/** Recovery is external, so nested errors must navigate the top-level control plane. */
export default function RecoveryLink({
  className = 'errbound__recovery',
  lead = 'If the problem continues after trying again,',
}) {
  // Every render site is a fallback for a surface that has already failed — a
  // crashed boundary, a crashed app host, or a startup error — so there is no
  // healthy app state to detect a deployment from. Naming both routes is always
  // correct: the owner picks the one that applies to their instance.
  return (
    <p className={className}>
      <span className="recovery-panel__recovery-lead">{lead}</span>
      <span className="recovery-panel__recovery-action">
        Managed hosting: <a href={RECOVERY_CONTROL_URL} target="_top">open Recovery in mobius.you</a>.
      </span>
      <span className="recovery-panel__recovery-action">
        Self-hosted — run this on the server:
      </span>
      <code className="recovery-panel__recovery-command">mobiusctl recovery</code>
    </p>
  )
}
