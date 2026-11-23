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

/** The stored rows of an answer's earlier replies, whose References its last
 *  reply lists. The same array while those replies' rows keep their identity,
 *  so an unchanged answer's reply is not re-rendered by a recompute. */
const earlierRowsByAnswer = new WeakMap()
function earlierReplyRows(groups) {
  const earlier = groups.slice(0, -1).map(group => group.presentationRows)
  const key = groups.at(-1).presentationRows
  const prior = earlierRowsByAnswer.get(key)
  if (prior && prior.earlier.length === earlier.length
    && prior.earlier.every((rows, index) => rows === earlier[index])) return prior.messages
  const messages = earlier.flatMap(rows => rows.map(row => row.message))
  earlierRowsByAnswer.set(key, { earlier, messages })
  return messages
}

/** Every complete answer's footer on the loaded page, and the current one.
 *
 *  The current answer is the live reply's group at `liveIndex` while one is
 *  shown (-1 otherwise), else the one holding the last visible row; an answer
 *  with a visible row after it is never current, so a stale live selection
 *  cannot keep it there. `state` is 'settled' while no turn is starting or
 *  running. While one is, the current answer is:
 *  - 'working' (its line kept, empty) while the same answer continues: the
 *    running run (`runningRoot`, its reply root) wrote it, or, before any run
 *    is named, an answered question of it (`questionPending`) is in flight.
 *    Keeping the line means settling fills it instead of adding it back.
 *  - 'working' when it is a reply the new turn writes: it ends after
 *    `settledEnd`, the absolute index of the settled footer reply's last row.
 *  - null when a new turn is leaving it, so the new reply's Thinking takes
 *    the line's place; the live surface can briefly reselect it then.
 *  An earlier answer is settled unless the running run wrote part of it: a
 *  visible steer ends the answer before it while that run continues, so it
 *  stays working until the run settles.
 *
 *  Each footer also lists `sourceRows`, the rows of its earlier replies whose
 *  References its last reply shows; `start` is the index of its first row
 *  after the owner's, so a hidden carrier opening it stays with it; and
 *  `truncated` says it may have begun before the loaded page (whose first row
 *  is at `offset`). */
export function replyFooters({
  messages, replyGroups, liveIndex = -1, lastVisibleIndex, offset = 0,
  turnStarting = false, settledEnd = null, runningRoot = null, questionPending = false,
}) {
  let currentGroup = replyGroups.get(liveIndex >= 0 ? liveIndex : lastVisibleIndex)
  if (currentGroup && currentGroup.end < lastVisibleIndex) currentGroup = null
  const byGroup = new Map()
  for (const [group, groups] of agentStretches(messages, replyGroups)) {
    let before = groups[0].start - 1
    while (before >= 0 && messages[before]?.hidden) before -= 1
    const end = offset + group.end
    const writtenByRun = !!runningRoot && groups.some(item => item.rows.some(
      row => assistantReplyRoot(row.message) === runningRoot))
    const continues = runningRoot ? writtenByRun : questionPending
    const state = group !== currentGroup ? (writtenByRun ? 'working' : 'settled')
      : !turnStarting ? 'settled'
        : continues || (liveIndex >= 0 && end !== settledEnd) ? 'working' : null
    byGroup.set(group, {
      group, groups, state, end, start: before + 1,
      truncated: before < 0 && offset > 0,
      sourceRows: earlierReplyRows(groups),
    })
  }
  return { byGroup, current: byGroup.get(currentGroup) || null }
}

/** What the stretch's replies say, in order, as each settled reply renders:
 *  every group projected as AssistantReply projects it (`positions` are the
 *  peer timeline's), then read by `replyProse`. A prose part names the
 *  rendered row that shows it by `key`, with its `block`; a card part has
 *  its `markdown` and its `card` text. Copy's text comes from here. */
export function stretchProse(groups, positions = new Map(), liveQuestionId = null) {
  return groups.flatMap(group => {
    const options = { positions }
    const rows = presentAssistantActivity(presentAssistantReply(group.presentationRows, options), options).rows
    return replyProse(rows.map(row => row.message), { liveQuestionId }).map(({ message, ...part }) => (
      Number.isInteger(part.block) ? { ...part, key: rows[message].key } : part))
  })
}

/** The replies Copy reads for `footer` (see replyFooters), or null while they
 *  are not known: the footer has not settled, or its stretch began before the
 *  loaded page and `startRows` (that start, see useStretchPrefix) is not read
 *  yet. `messages` is the transcript's peer timeline, `offset` its first
 *  row's index, `slots` its peer notes and `displayKeys` its row keys. A
 *  stretch wholly on the page is the transcript's own reply groups; one with
 *  a read start is regrouped with that start before the loaded rows, with the
 *  same keys and peer-note slots, so a reply split across the page's edge
 *  reads as one and both cases project exactly as the transcript does. */
function stretchReplies(footer, { startRows, messages, offset = 0, slots = new Map(), displayKeys }) {
  if (footer?.state !== 'settled') return null
  if (!footer.truncated) return footer.groups
  if (!startRows) return null
  const shift = startRows.length
  const rows = [...startRows, ...messages.slice(0, footer.group.end + 1)]
  const groups = assistantReplyGroups(rows, {
    offset: offset - shift, displayKeys,
    slots: new Map([...slots].map(([index, notes]) => [index + shift, notes])),
  })
  return [...new Set(groups.values())]
}

/** Copy's parts for `footer`, or null while its replies are not known (see
 *  stretchReplies, which takes the rest of `context`). `positions` and
 *  `liveQuestionId` (the chat's open question) are the peer timeline's. */
export function footerCopyParts(footer, { positions, liveQuestionId, ...context }) {
  const groups = stretchReplies(footer, context)
  return groups && stretchProse(groups, positions, liveQuestionId)
}

/** Whether a settled answer offers Copy: the same replies Copy reads (see
 *  stretchReplies), each read by replyProse on its stored rows. Presentation
 *  only joins and regroups a reply's prose, so it cannot decide whether there
 *  is any; the text itself is built when Copy is pressed (footerCopyParts).
 *  The answer is remembered on exactly what that reads: its replies' rows
 *  (unchanged while a later answer streams), or, for one that began before
 *  the loaded page, the read start and the page's rows through the answer,
 *  and the open question. */
const copyableByAnswer = new WeakMap()
export function footerHasCopy(footer, { startRows = null, messages, liveQuestionId = null } = {}) {
  if (footer?.state !== 'settled' || (footer.truncated && !startRows)) return false
  const inputs = footer.truncated ? [startRows, ...messages.slice(0, footer.group.end + 1)]
    : footer.groups.map(group => group.presentationRows)
  const prior = copyableByAnswer.get(footer.group.presentationRows)
  if (prior && prior.liveQuestionId === liveQuestionId && prior.inputs.length === inputs.length
    && prior.inputs.every((item, index) => item === inputs[index])) return prior.copyable
  const copyable = stretchReplies(footer, { startRows, messages }).some(group => (
    replyProse(group.presentationRows.map(row => row.message), { liveQuestionId }).length > 0))
  copyableByAnswer.set(footer.group.presentationRows, { inputs, liveQuestionId, copyable })
  return copyable
}
