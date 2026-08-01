export const RECOVERY_PATH = '/recover'

export default function RecoveryLink({
  className = 'errbound__recovery',
  lead = 'If the problem continues after trying again,',
}) {
  return (
    <p className={className}>
      {lead} <a href={RECOVERY_PATH}>open recovery</a>.
    </p>
  )
}
