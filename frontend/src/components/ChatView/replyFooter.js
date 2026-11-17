/* One footer per complete answer stretch, with no footer on internal runs:
   which replies hold one, in what state, and what each copies (everything the
   agent said since the preceding visible owner row). Pure, so ChatView only
   wires its result. */
import { assistantReplyGroups, presentAssistantActivity, presentAssistantReply } from './assistantReplies.js'
import { replyProse } from './messageCopy.js'
import { assistantReplyRoot } from './steerContinuity.js'

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

/** Every complete answer's footer on the loaded page, and the current one.
 *
 *  The current answer is the live reply's group at `liveIndex` while one is
 *  shown (-1 otherwise), else the one holding the last visible row. `state`
 *  is 'settled' while no turn is starting or running. While one is, the
 *  current answer is 'working' (an empty, reserved line) when the turn writes
 *  it, and null when it is the answer that held the footer as the turn
 *  started, which the live surface can briefly reselect before the turn's
 *  reply exists. `settledEnd` (the absolute index of the settled footer
 *  reply's last row) tells them apart: a turn continuing that same reply adds
 *  a row after it. An earlier answer is settled unless the running run
 *  (`runningRoot`, its reply root) wrote part of it: a visible steer ends the
 *  answer before it while that run continues, so it stays working until the
 *  run settles.
 *
 *  Each footer also lists `sourceRows`, the rows of its earlier replies whose
 *  References its last reply shows; `start` is the index of its first row
 *  after the owner's, so a hidden carrier opening it stays with it; and
 *  `truncated` says it may have begun before the loaded page (whose first row
 *  is at `offset`). */
export function replyFooters({
  messages, replyGroups, liveIndex = -1, lastVisibleIndex, offset = 0,
  turnStarting = false, settledEnd = null, runningRoot = null,
}) {
  const currentGroup = replyGroups.get(liveIndex >= 0 ? liveIndex : lastVisibleIndex)
  const byGroup = new Map()
  for (const [group, groups] of agentStretches(messages, replyGroups)) {
    let before = groups[0].start - 1
    while (before >= 0 && messages[before]?.hidden) before -= 1
    const end = offset + group.end
    const runContinues = !!runningRoot && groups.some(item => item.rows.some(
      row => assistantReplyRoot(row.message) === runningRoot))
    const state = group !== currentGroup ? (runContinues ? 'working' : 'settled')
      : !turnStarting ? 'settled'
        : liveIndex >= 0 && end !== settledEnd ? 'working' : null
    byGroup.set(group, {
      group, groups, state, end, start: before + 1,
      truncated: before < 0 && offset > 0,
      sourceRows: groups.slice(0, -1).flatMap(item => item.rows.map(row => row.message)),
    })
  }
  return { byGroup, current: byGroup.get(currentGroup) || null }
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

/** Copy's parts for `footer` (see replyFooters), or null while they are not
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

/** Whether a settled answer offers Copy: the same prose reading Copy uses
 *  (replyProse) on the answer's stored rows, and, for an answer that began
 *  before the loaded page, only once its start (`startRows`) is read.
 *  Presentation only joins and regroups that prose, so it cannot decide
 *  whether there is any; the text itself is built when Copy is pressed (see
 *  footerCopyParts). replyProse reads only those messages and the open
 *  question, so the answer is remembered on exactly those: its rows' identity
 *  (unchanged while a later answer streams), the read start and the question. */
const copyableByRows = new WeakMap()
export function footerHasCopy(footer, { startRows = null, liveQuestionId = null } = {}) {
  if (footer?.state !== 'settled') return false
  if (footer.truncated && !startRows) return false
  const rows = footer.groups.map(group => group.presentationRows)
  const prefix = footer.truncated ? startRows : null
  const prior = copyableByRows.get(footer.group.presentationRows)
  if (prior && prior.prefix === prefix && prior.liveQuestionId === liveQuestionId
    && prior.rows.length === rows.length && prior.rows.every((item, index) => item === rows[index])) {
    return prior.copyable
  }
  const messages = [...(prefix || []), ...rows.flatMap(items => items.map(row => row.message))]
  const copyable = replyProse(messages, { liveQuestionId }).length > 0
  copyableByRows.set(footer.group.presentationRows, { rows, prefix, liveQuestionId, copyable })
  return copyable
}
