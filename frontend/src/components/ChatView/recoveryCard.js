/* Pure ownership policy for the one actionable recovery card at transcript tail. */

export function ownsRecoveryAction({
  block,
  entryIndex,
  lastEntryIndex,
  isLastMessage,
  canResume,
  questionOwnsTurn = false,
}) {
  return !!(
    block?.resumable
    && !questionOwnsTurn
    && isLastMessage
    && canResume
    && entryIndex === lastEntryIndex
  )
}
