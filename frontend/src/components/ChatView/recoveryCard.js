/* Pure ownership policy for the one actionable recovery card at transcript tail. */

export function ownsRecoveryAction({
  block,
  entryIndex,
  lastEntryIndex,
  isLastMessage,
  canResume,
}) {
  return !!(
    block?.resumable
    && isLastMessage
    && canResume
    && entryIndex === lastEntryIndex
  )
}
