/* Hidden cuts preserve continuous prose, source identity, and one reference footer. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { assistantReplyGroups, presentAssistantReply, replyQuestionSuppression } from '../assistantReplies.js'
import { messageSources } from '../messageSources.js'
import { assistantReplyRoot, projectSettledSteerContinuations, projectSteerContinuationMessage } from '../steerContinuity.js'
const assistant = (content, n = 0, extras = {}) => ({ role: 'assistant', id: n ? `run:assistant:${n}` : 'run', content, blocks: [{ type: 'text', content }], ...extras })
const carrier = (kind = 'peer_message', extras = {}) => ({ role: 'user', hidden: true, steered: true, source_work_id: 'run', kind, ...extras })
const rows = messages => assistantReplyGroups(messages).get(0).rows
const text = row => row.message.blocks.filter(b => b.type === 'text').map(b => b.content).join('')

test('history prepend retains settled reply projection inputs at stable absolute keys', () => {
 const old = { role: 'assistant', id: 'older', content: 'Older' }
 const current = [assistant('First'), carrier(), assistant('First continued', 1)]
 const beforeGroups = assistantReplyGroups(current, { offset: 10 })
 const before = beforeGroups.get(0)
 const after = assistantReplyGroups([old, ...current], {
  offset: 9, previousGroups: beforeGroups,
 }).get(1)
 assert.notEqual(after, before, 'current physical indices belong to the new page')
 assert.deepEqual(after.rows.map(row => row.index), [1, 3])
 assert.deepEqual(after.rows.map(row => row.key), before.rows.map(row => row.key))
 assert.equal(after.presentationRows, before.presentationRows)

 const changed = assistantReplyGroups([old, current[0], current[1], { ...current[2], content: 'Changed' }], {
  offset: 9, previousGroups: new Map([[1, after]]),
 }).get(1)
 assert.notEqual(changed.presentationRows, before.presentationRows,
  'a changed saved reply must recompute its displayed content')

 const withNotes = assistantReplyGroups([old, ...current], {
  offset: 9, previousGroups: new Map([[1, after]]),
  slots: new Map([[2, [{ id: 'peer-note' }]]]),
 }).get(1)
 assert.notEqual(withNotes.presentationRows, after.presentationRows,
  'a newly projected peer boundary must not reuse an old reply layout')

 const active = assistantReplyGroups([old, ...current], {
  offset: 9, previousGroups: new Map([[1, after]]), activeIndex: 3,
  activeKey: 'live-row-key',
 }).get(1)
 assert.notEqual(active.presentationRows, after.presentationRows,
  'a live-to-saved display key handoff must not reuse the old anchor')
})

for (const provider of ['claude', 'codex', 'mobius', 'responses']) {
 test(`${provider}: an exact replay continues the original paragraph across a hidden cut`, () => {
  const messages = [assistant('This sentence continues;', 0, { provider }), carrier('delegation_result'), assistant('This sentence continues; without an artificial paragraph boundary.', 1, { provider })]
  const before = JSON.stringify(messages)
  const shown = presentAssistantReply(rows(messages))
  assert.equal(text(shown[0]), 'This sentence continues; without an artificial paragraph boundary.')
  assert.equal(text(shown[1]), '')
  assert.equal(shown[0].key, 'run')
  assert.equal(shown[1].key, 'run:assistant:1')
  assert.equal(shown[1].message.reply_text_owner_key, 'run', 'suffix search maps to its displayed text owner')
  assert.equal(JSON.stringify(messages), before)
 })
}

test('chained hidden replay grows one original text owner without duplicates', () => {
 const shown = presentAssistantReply(rows([assistant('The frame'), carrier(), assistant('The framework', 1), carrier(), assistant('The framework survives.', 2)]))
 assert.deepEqual(shown.map(text), ['The framework survives.', '', ''])
})

test('legacy id-less hidden steers keep safe replay suppression; visible steers do not defer it', () => {
 const before = assistant('Prefix', 0, { id: undefined })
 const after = assistant('Prefix suffix', 1, { id: undefined })
 const projected = projectSettledSteerContinuations([before, carrier(), after], { preserveHidden: true })
 assert.equal(projected[2].blocks[0].content, ' suffix')
 const mixed = [assistant('Prefix'), carrier('peer_message', { hidden: false }), carrier(), assistant('Prefix suffix', 1)]
 assert.equal(projectSettledSteerContinuations(mixed, { preserveHidden: true })[3].blocks[0].content, ' suffix')
})

test('unmirrored live identity groups immediately; its display key survives the DB handoff', () => {
 const before = [assistant('The frame'), carrier()]
 const live = assistant('The framework', 1)
 const options = { offset: 8, activeIndex: 2, activeKey: 'assistant-10' }
 const virtual = assistantReplyGroups([...before, live], options).get(0)
 const mirror = assistantReplyGroups([...before, { ...live, ts: 123 }], options).get(0)
 assert.equal(virtual.rows.length, 2)
 assert.deepEqual(virtual.rows.map(row => [row.key, row.anchorKey]), mirror.rows.map(row => [row.key, row.anchorKey]))
 assert.equal(virtual.rows[1].message.id, 'run:assistant:1')
 assert.equal(virtual.rows[1].key, 'assistant-10')
 assert.equal(presentAssistantReply(virtual.rows, { activeIndex: 1 })[0].message.blocks[0].content, 'The framework')
})

test('sealed and settled sources retain the first-painted live display keys', () => {
 const messages = [assistant('Prefix'), carrier(), assistant('Prefix suffix', 1)]
 const displayKeys = new Map([['run', 'assistant-0'], ['run:assistant:1', 'assistant-3']])
 const sealed = assistantReplyGroups(messages, { displayKeys, activeIndex: 2, activeKey: 'assistant-3' }).get(0)
 const settled = assistantReplyGroups(messages, { displayKeys }).get(0)
 assert.deepEqual(sealed.rows.map(row => row.key), ['assistant-0', 'assistant-3'])
 assert.deepEqual(settled.rows.map(row => row.key), sealed.rows.map(row => row.key))
 assert.equal(presentAssistantReply(settled.rows)[1].message.reply_text_owner_key, 'assistant-0')
})

test('multiple hidden carriers keep timeline notes; a folded source cannot swallow References', () => {
 const messages = [assistant('First', 0, { source_ref: { message_index: 0, count: 1 } }), carrier(), carrier('delegation_result'), assistant('First continued', 1), carrier(), assistant('', 2, { hidden: true, source_ref: { message_index: 5, count: 1 } })]
 const notes = [{ id: 'peer' }]
 const group = assistantReplyGroups(messages, { slots: new Map([[2, notes]]) }).get(0)
 assert.equal(group.rows.length, 3)
 assert.deepEqual(group.rows[1].notes, notes)
 const shown = presentAssistantReply(group.rows)
 assert.deepEqual(group.rows.flatMap(row => row.message.source_ref ? [row.message.source_ref.message_index] : []), [0, 5])
 assert.equal(shown[2].message.hidden, true)
})

test('a hidden final source leaves terminal controls on the preceding visible reply', () => {
 const group = assistantReplyGroups([assistant('Saved work'), carrier(), assistant('', 1, { hidden: true })]).get(0)
 assert.equal(group.end, 2, 'the hidden source retains its physical index')
 assert.equal(group.lastVisibleIndex, 0)
 assert.deepEqual(group.rows.map(row => row.key), ['run', 'run:assistant:1'])
})

test('only positive ASCII sink segments share a physical-run identity', () => {
 const first = assistant('Prefix')
 for (const suffix of ['0', '01', '-1', '١', 'notes']) {
  const next = assistant('Prefix continued', 1, { id: `run:assistant:${suffix}` })
  assert.equal(assistantReplyRoot(next), next.id)
  assert.equal(assistantReplyGroups([first, carrier(), next]).get(0).rows.length, 1)
  assert.equal(projectSteerContinuationMessage(first, next), next)
 }
 assert.equal(assistantReplyRoot(assistant('', 12)), 'run')
})

test('live prefix replay never hides the original and divergence reveals its entire text', () => {
 for (const [next, expected] of [['The fra', ''], ['New plan', 'New plan']]) {
  const shown = presentAssistantReply(rows([assistant('The frame'), carrier(), assistant(next, 1)]), { activeIndex: 1 })
  assert.equal(text(shown[0]), 'The frame')
  assert.equal(text(shown[1]), expected)
 }
 const grown = presentAssistantReply(rows([assistant('The frame'), carrier(), assistant('The framework', 1)]), { activeIndex: 1 })
 assert.equal(text(grown[0]), 'The framework')
 assert.equal(grown[0].message.blocks[0].reply_live_text, true)
})

test('suffix-only and settled short messages stay lossless, never guessed into prose', () => {
 for (const suffix of [' suffix-only', 'The fra', '\n\nA real new paragraph.']) {
  const shown = presentAssistantReply(rows([assistant('The frame'), carrier(), assistant(suffix, 1)]))
  assert.equal(text(shown[0]), 'The frame')
  assert.equal(text(shown[1]), suffix)
 }
})

test('fused prose keeps its Markdown surface but no cursor while later activity runs', () => {
 const current = assistant('Prefix suffix', 1, { blocks: [{ type: 'text', content: 'Prefix suffix' }, { type: 'tool', tool: 'Bash', status: 'running' }] })
 const shown = presentAssistantReply(rows([assistant('Prefix'), carrier(), current]), { activeIndex: 1 })
 assert.equal(shown[0].message.blocks[0].reply_text_owner, true)
 assert.equal(shown[0].message.blocks[0].reply_live_text, false)
 assert.equal(shown[1].message.blocks[1].status, 'running')
})

test('real owner messages, ordinary hidden inputs and different runs remain separate replies', () => {
 for (const boundary of [carrier('peer_message', { hidden: false }), carrier('peer_message', { steered: false }), carrier('peer_message', { source_work_id: 'another' }), { role: 'user', content: 'owner' }]) {
  const groups = assistantReplyGroups([assistant('Prefix'), boundary, assistant('Prefix suffix', 1)])
  assert.equal(groups.get(0).rows.length, 1)
  assert.equal(groups.get(2).rows.length, 1)
 }
 const groups = assistantReplyGroups([assistant('Prefix'), carrier(), assistant('Prefix suffix', 1, { id: 'other:assistant:1' })])
 assert.equal(groups.get(0).rows.length, 1)
 assert.equal(groups.get(2).rows.length, 1)
})

test('missing cold predecessor renders full text; loading it yields the same visible sentence', () => {
 const full = assistant('Prefix suffix', 1)
 assert.equal(text(presentAssistantReply(rows([full]))[0]), 'Prefix suffix')
 const loaded = presentAssistantReply(rows([assistant('Prefix'), carrier(), full]))
 assert.equal(loaded.map(text).join(''), 'Prefix suffix')
})

test('thoughts, tools, cards and timeline notes preserve the actual interruption position', () => {
 const thought = { type: 'thinking', content: 'Think', thinking_id: 'think' }
 for (const beat of [thought, { type: 'tool', tool: 'Bash' }, { type: 'question', question_id: 'q' }]) {
  const next = assistant('Prefix suffix', 1, { blocks: [beat, { type: 'text', content: 'Prefix suffix' }] })
  const shown = presentAssistantReply(rows([assistant('Prefix'), carrier(), next]))
  assert.equal(text(shown[0]), 'Prefix')
  assert.equal(shown[1].message.blocks[0], beat)
 }
 const messages = [assistant('Prefix'), carrier(), assistant('Prefix suffix', 1)]
 const notes = [{ id: 'helper', type: 'helper_result' }]
 const group = assistantReplyGroups(messages, { slots: new Map([[1, notes]]) }).get(0)
 const shown = presentAssistantReply(group.rows)
 assert.equal(text(shown[0]), 'Prefix')
 assert.equal(text(shown[1]), ' suffix')
 assert.deepEqual(shown[1].notes, notes)
 const positioned = presentAssistantReply(rows(messages), { positions: new Map([['run', [{ display_position: { block_index: 0, text_offset: 6 } }]]]) })
 assert.equal(text(positioned[0]), 'Prefix')
 assert.equal(text(positioned[1]), ' suffix')
})

test('unsafe Markdown preserves complete source and genuine paragraphs survive fusion', () => {
 const unsafe = presentAssistantReply(rows([assistant('**Prefix'), carrier(), assistant('**Prefix** suffix', 1)]))
 assert.equal(text(unsafe[0]), '**Prefix')
 assert.equal(text(unsafe[1]), '**Prefix** suffix')
 const safe = presentAssistantReply(rows([assistant('First\n\nSecond'), carrier(), assistant('First\n\nSecond continues.\n\nThird.', 1)]))
 assert.equal(text(safe[0]), 'First\n\nSecond continues.\n\nThird.')
})

test('reply projection preserves original source indices and inline metadata', () => {
 const tool = url => ({ type: 'tool', sources: [{ url }] })
 const messages = [assistant('First', 0, { blocks: [tool('https://a.example'), { type: 'text', content: 'First' }], source_ref: { message_index: 7, count: 2 } }), carrier(), assistant('First continued', 1, { blocks: [{ type: 'text', content: 'First continued' }, tool('https://a.example'), tool('https://b.example')], source_ref: { message_index: 9, count: 2 } })]
 const shown = presentAssistantReply(rows(messages))
 assert.deepEqual(shown.map(row => row.message.source_ref.message_index), [7, 9])
 assert.equal(new Set(shown.flatMap(row => messageSources(row.message.blocks)).map(s => s.url)).size, 2)
 assert.equal(shown[0].message.blocks[0], messages[0].blocks[0], 'original tool addressing survives')
})


test('message-level outcome and wake cards remain between the original and continued prose', () => {
 for (const [before, after] of [
  [{ goal_summaries: [{ id: 'goal' }] }, {}],
  [{ wait_summaries: [{ id: 'stopped-wait' }] }, {}],
  [{}, { continuation_reason: 'recovery' }],
  [{}, { wait_summaries: [{ id: 'wake' }] }],
 ]) {
  const shown = presentAssistantReply(rows([assistant('Prefix', 0, before), carrier(), assistant('Prefix suffix', 1, after)]))
  assert.deepEqual(shown.map(text), ['Prefix', ' suffix'])
 }
})


test('the single selected reply source never suppresses its own question card', () => {
 const liveQuestions = new Set(['question_id:q-accepted'])
 assert.equal(replyQuestionSuppression(liveQuestions, 0, 0), null,
   'an accepted saved/live source still paints Submitted while catch-up retains the key')
 assert.equal(replyQuestionSuppression(liveQuestions, 1, 0), liveQuestions,
   'a parallel durable row still deduplicates the active live card')
 assert.equal(replyQuestionSuppression(null, -1, 0), null,
   'settled history is never hidden after the live surface retires')
})
