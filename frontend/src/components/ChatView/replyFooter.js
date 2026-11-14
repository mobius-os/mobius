/* The chat's one reply footer: which reply holds it, in what state, and the
   agent's stretch it copies (everything the agent said since the owner last
   spoke). Pure, so ChatView only wires its result. */
import { assistantReplyGroups, presentAssistantActivity, presentAssistantReply } from './assistantReplies.js'
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
 *  its `markdown` and its `card` text. Copy's visibility and its text both come from here. */
export function stretchProse(groups, positions = new Map(), liveQuestionId = null) {
  return groups.flatMap(group => {
    const options = { positions }
    const rows = presentAssistantActivity(presentAssistantReply(group.presentationRows, options), options).rows
    return replyProse(rows.map(row => row.message), { liveQuestionId }).map(({ message, ...part }) => (
      Number.isInteger(part.block) ? { ...part, key: rows[message].key } : part))
  })
}

/** Copy's parts for `footer` (see replyFooter), or null while they are not
 *  known: the footer has not settled, or its stretch began before the loaded
 *  page and `startRows` (that start, see useStretchPrefix) is not read yet.
 *  `messages`, `slots` and `positions` are the transcript's peer timeline,
 *  `offset` its first row's index, `displayKeys` its row keys and
 *  `liveQuestionId` the chat's open question. A stretch
 *  wholly on the page uses the transcript's own reply groups; one with a read
 *  start is regrouped with that start before the loaded rows, with the same
 *  peer-note slots, so a reply split across the page's edge copies as one and
 *  both cases project exactly as the transcript does. */
export function footerCopyParts(footer, {
  startRows, messages, offset = 0, slots = new Map(), positions, displayKeys, liveQuestionId,
}) {
  if (footer?.state !== 'settled') return null
  if (!footer.truncated) return stretchProse(footer.groups, positions, liveQuestionId)
  if (!startRows) return null
  const shift = startRows.length
  const rows = [...startRows, ...messages.slice(0, footer.group.end + 1)]
  const groups = assistantReplyGroups(rows, {
    offset: offset - shift, displayKeys,
    slots: new Map([...slots].map(([index, notes]) => [index + shift, notes])),
  })
  return stretchProse([...new Set(groups.values())], positions, liveQuestionId)
}
