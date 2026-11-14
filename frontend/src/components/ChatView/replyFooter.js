/* The chat's one reply footer: which reply holds it, in what state, and the
   agent's stretch it copies (everything the agent said since the owner last
   spoke). Pure, so ChatView only wires its result. */
import { presentAssistantActivity, presentAssistantReply } from './assistantReplies.js'
import { replyProse } from './messageCopy.js'

/** The agent's stretches: each reply that ends one, mapped to the replies in
 * it, oldest first. A stretch runs until the next visible row that is not a
 * reply (an owner message or a visible marker). Answered cards, waits and
 * helper results are hidden carriers, so it runs on across them, and the live
 * reply's placeholder is a group, so it extends its stretch while it runs. */
export function agentStretches(messages, replyGroups) {
  const stretches = new Map()
  let stretch = []
  for (const group of new Set(replyGroups.values())) {
    stretch.push(group)
    let next = group.end + 1
    while (messages[next]?.hidden) next += 1
    if (replyGroups.has(next)) continue
    stretches.set(group, stretch)
    stretch = []
  }
  return stretches
}

/** The footer, or null when the chat does not end on a reply.
 *
 *  It sits on the reply at the very end of the chat: the live reply's group
 *  at `liveIndex` while one is shown (-1 otherwise), else the one holding the
 *  last visible row. `state` is 'settled' while no turn is starting or
 *  running; while one is, 'working' (an empty, reserved line) on a reply the
 *  turn is writing, and null on the reply that held the footer when it
 *  started, which the live surface can briefly reselect before the turn's
 *  reply exists. That reply is told apart by where it ends: `settledEnd` is
 *  the absolute index of the settled footer reply's last row, and a turn
 *  continuing that same reply adds a row after it.
 *
 *  `start` is the index of the stretch's first row after the owner's, so a
 *  hidden carrier opening it stays with it; `truncated` says the stretch may
 *  have begun before the loaded page (whose first row is at `offset`). */
export function replyFooter({
  messages, replyGroups, stretches, liveIndex = -1, lastVisibleIndex, offset = 0,
  turnStarting = false, settledEnd = null,
}) {
  const group = replyGroups.get(liveIndex >= 0 ? liveIndex : lastVisibleIndex)
  const groups = group && stretches.get(group)
  if (!groups) return null
  let before = groups[0].start - 1
  while (before >= 0 && messages[before]?.hidden) before -= 1
  const end = offset + group.end
  const state = !turnStarting ? 'settled'
    : liveIndex >= 0 && end !== settledEnd ? 'working' : null
  return { group, groups, state, end, start: before + 1, truncated: before < 0 && offset > 0 }
}

/** What the stretch's replies say, in order, as each settled reply renders:
 *  every group projected as AssistantReply projects it (`positions` are the
 *  peer timeline's), then read by `replyProse`. A prose part names the
 *  rendered row that shows it by `key`, with its `block`; a card part has
 *  only its `markdown`. Copy's visibility and its text both come from here. */
export function stretchProse(groups, positions = new Map()) {
  return groups.flatMap(group => {
    const options = { positions }
    const rows = presentAssistantActivity(presentAssistantReply(group.presentationRows, options), options).rows
    return replyProse(rows.map(row => row.message)).map(({ message, ...part }) => (
      Number.isInteger(part.block) ? { ...part, key: rows[message].key } : part))
  })
}
