export const RECOVERY_PATH = '/recover'

/** Recovery denies framing, so nested chat errors must navigate the top-level page. */
export default function RecoveryLink({
  className = 'errbound__recovery',
  lead = 'If the problem continues after trying again,',
}) {
  return (
    <p className={className}>
      {lead} <a href={RECOVERY_PATH} target="_top">open the isolated recovery workspace</a>.
    </p>
  )
}
