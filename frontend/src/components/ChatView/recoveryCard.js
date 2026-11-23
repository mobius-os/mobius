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

// A direct provider switch ends the paused turn's transcript with its own row,
// so the pause card is no longer the tail and loses its Resume button. The
// switch row inherits that one action while it is the tail and the row right
// before it still ends with a resumable pause.
export function ownsSwitchResume({
  msg,
  previousMsg,
  isLastMessage,
  canResume,
  questionOwnsTurn = false,
}) {
  const previousTail = previousMsg?.blocks?.at(-1)
  return !!(
    isDirectSwitchRow(msg)
    && previousTail?.resumable
    && !questionOwnsTurn
    && isLastMessage
    && canResume
  )
}

// The visible row a switch card at `index` follows. Earlier switch cards
// directly before it are passed over, so the paused turn is still found when
// the chat was switched twice in a row.
export function previousVisibleMessage(messages, index) {
  for (let i = index - 1; i >= 0; i -= 1) {
    const row = messages[i]
    if (row.hidden || isDirectSwitchRow(row)) continue
    return row
  }
  return null
}

function isDirectSwitchRow(msg) {
  return msg?.kind === 'compaction' && msg.handover === 'direct'
}
