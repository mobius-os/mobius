/* Rendered lifecycle identities must remain distinct after the same outcome. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement as h } from 'react'
import { renderToStaticMarkup as render } from 'react-dom/server'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
const previousWindow = globalThis.window
globalThis.window = { location: { href: 'http://localhost/' } }
after(() => { globalThis.window = previousWindow })
const vite = await createSsrRenderServer()
after(() => vite.close())
const load = async name => (await vite.ssrLoadModule('/src/components/ChatView/' + name + '.jsx')).default
const Goal = await load('GoalHistoryCard')
const Wait = await load('WaitHistoryCard')
const Resume = await load('ContinuationCard')
const ErrorCard = await load('ErrorCard')
const PauseRecord = await load('PauseRecordCard')
const icon = html => html.match(/<span class="chat__lifecycle-icon"[\s\S]*?<\/span>/)?.[0]

test('Goal and Wait keep different identity icons even when both complete', () => {
 const goal = render(h(Goal, { summary: { objective: 'Ship', status: 'completed' } }))
 const wait = render(h(Wait, { summary: { description: 'Deployment ready', status: 'met' } }))
 assert.ok(icon(goal)); assert.ok(icon(wait))
 assert.notEqual(icon(goal), icon(wait))
 for (const html of [goal, wait]) assert.doesNotMatch(html, /class="chat__lifecycle-outcome"/)
})
test('Waiting preserves its clock across completed, attention and stopped states', () => {
 const states = ['met', 'expired', 'failed', 'cancelled'].map(status => render(h(Wait, { summary: { description: 'Deployment ready', status } })))
 for (const html of states) assert.equal(icon(html), icon(states[0]))
 for (const html of states.slice(1)) assert.match(html, /class="chat__lifecycle-outcome"/)
})
test('resume, pause and errors share icon geometry without sharing meaning', () => {
 const resumed = render(h(Resume, { msg: { continuation_reason: 'manual' } }))
 const paused = render(h(ErrorCard, { block: { pause: { kind: 'restart' }, resumable: true } }))
 const failed = render(h(ErrorCard, { block: { message: 'Unavailable' } }))
 assert.ok(icon(resumed)); assert.ok(icon(paused)); assert.ok(icon(failed))
 assert.notEqual(icon(paused), icon(failed))
 assert.match(resumed, /Resumed manually/); assert.match(paused, /Paused/); assert.match(failed, /role="alert"/)
})

test('automatic resume markers explain what caused the continuation', () => {
 const restarted = render(h(Resume, { msg: { continuation_reason: 'restart' } }))
 const goalHandoff = render(h(Resume, { msg: { continuation_reason: 'goal_handoff' } }))
 const recovered = render(h(Resume, { msg: {} }))

 assert.match(restarted, /Server restarted — continuing automatically/)
 assert.match(goalHandoff, /Continuing Goal/)
 assert.match(goalHandoff, /Continuing toward the Goal/)
 assert.match(recovered, /Interrupted work recovered — continuing automatically/)
})

test('in-conversation Waiting follows the compact handoff identity instead of a tile or badge', async () => {
 const { WaitCard } = await vite.ssrLoadModule('/src/components/ChatView/WaitingChip.jsx')
 const html = render(h(WaitCard, { wait: { id: 'sample', kind: 'condition', description: 'Review approved' }, expanded: false, onToggle: () => {}, onCancel: () => {} }))
 assert.match(html, /chat__progress-identity/)
 assert.match(html, /Waiting · Review approved/)
 assert.doesNotMatch(html, /chat__lifecycle-icon|chat__wait-tag/)
 assert.match(html, /aria-expanded="false"/)
})

test('Brain network summary opens a separate view and shows mailbox totals', async () => {
 const { QueryClient, QueryClientProvider } = await import('@tanstack/react-query')
 const Network = await load('ChatAgentNetwork')
 const client = new QueryClient()
 client.setQueryData(['chat-network-summary', 'standalone-chat'], { total: 12, sent: 5, received: 7 })
 const html = render(h(QueryClientProvider, { client }, h(Network, { chatId: 'standalone-chat' })))
 assert.match(html, /12 messages · 5 sent · 7 received/)
 assert.match(html, /aria-haspopup="dialog"/)
 assert.doesNotMatch(html, /agent-relay|aria-expanded/)
 client.clear()
})

test('network messages expose routing, broadcast audience and full safe text', async () => {
 const { NetworkMessage } = await vite.ssrLoadModule('/src/components/ChatView/ChatNetworkInspector.jsx')
 const html = render(h(NetworkMessage, { chatId: 'self', message: { id: '1', sender_chat_id: 'peer', sender_name: 'Scout', broadcast: true, room_kind: 'project', kind: 'blocker', body: '<script>private note</script>' } }))
 assert.match(html, /Received from /)
 assert.match(html, /Broadcast/)
 assert.doesNotMatch(html, /<dl|<dt|<dd/)
 assert.match(html, /Scout/)
 assert.match(html, /Project group/)
 assert.match(html, /blocker/)
 assert.match(html, /&lt;script&gt;/)
 const sent = render(h(NetworkMessage, { chatId: 'self', message: { sender_chat_id: 'self', recipient_name: 'Builder', body: 'Ready', kind: 'handoff' } }))
 assert.match(sent, /Sent to /); assert.match(sent, /Builder/); assert.doesNotMatch(sent, /This chat|<dl/)
})

test('expanded Goal tasks have no dependency on the agent network query', async () => {
 const Details = await load('GoalPlanDetails')
 const html = render(h(Details, { plan: { tasks: [{ id: 'task', title: 'Verify deployment', status: 'running' }] } }))
 assert.match(html, /Verify deployment/)
 assert.doesNotMatch(html, /Agent network|agent-relay/)
})

// A scheduled check is not evidence that the provider has restored quota.
test('provider-limit continuation reports a retry without promising availability', () => {
 const html = render(h(Resume, { msg: { continuation_reason: 'usage_limit' } }))
 assert.match(html, /Resumed automatically/)
 assert.match(html, /Retry check due — trying the provider again/)
 assert.doesNotMatch(html, /[Uu]sage available|[Ll]imit (has )?reset|[Qq]uota restored/)
})

test('foreign decision has one exact Open decision link and no answer control', async () => {
 const Details = await load('GoalPlanDetails')
 const dependency = { id: 'decision', target_kind: 'owner_card', condition: 'Release choice', source_chat_id: 'source', source_question_id: 'card-1', source_chat_title: 'Repair', automatic: true }
 const html = render(h(Details, { dependencies: [dependency] }))
 assert.match(html, /Release choice/); assert.match(html, /Open decision/)
 assert.match(html, /href="\/shell\/\?chat=source&amp;focus=question&amp;question=card-1"/)
 assert.match(html, />Open decision · Repair<\/a>/)
 assert.doesNotMatch(html, /<input|<button|deadline|next check|Waiting for you/)
 const deleted = render(h(Details, { dependencies: [{ ...dependency, source_deleted: true }] }))
 assert.doesNotMatch(deleted, /<a /)
 assert.match(deleted, /The chat was deleted/)
})

test('decision history preserves quiet close and never invents an answer start', async () => {
 const Details = await load('GoalPlanDetails')
 const dependency = { id: 'decision', target_kind: 'owner_card', condition: 'Release choice', source_chat_title: 'Repair', disposition: 'retired_by_goal_outcome', received_run_id: null, outcome: { kind: 'closed_without_reply', answers: { release: 'Not now' } } }
 const html = render(h(Details, { dependencyHistory: [dependency] }))
 assert.match(html, /You chose Not now/)
 assert.doesNotMatch(html, /Continues automatically|Started|Waiting ·|resumed/)
})

// Manual and automatic recovery each leave one durable product notice.
test('manual continuation records interrupted work without another pause card', () => {
 const html = render(h(Resume, { msg: { continuation_reason: 'manual' } }))
 assert.match(html, /Resumed manually/)
 assert.match(html, /Interrupted work continued/)
 assert.doesNotMatch(html, /Paused · Continued|automatically|>Resume</)
})

test('a settled pause names its cause and offers no recovery action', () => {
 const record = source => render(h(PauseRecord, { block: { type: 'pause_record', source } }))
 const update = record({ type: 'error', resumable: true, pause: { kind: 'restart' } })
 const storage = record({ type: 'error', resumable: true, pause: { kind: 'storage' } })
 const legacyLimit = record({ type: 'error', resumable: true, pause: { kind: 'limit' } })
 const crash = record({ type: 'error', resumable: true, message: 'Interrupted.' })
 assert.match(update, /Paused for a platform update/)
 assert.match(storage, /Paused while storage was low/)
 assert.match(legacyLimit, /Paused at a usage limit/, 'legacy notes keep the live card classification')
 assert.match(crash, /Work interrupted/)
 for (const html of [update, storage, legacyLimit, crash]) {
  assert.doesNotMatch(html, /<button|role="alert"|automatically/)
 }
})

test('a resume marker shows when the work continued', () => {
 const today = new Date(); today.setHours(9, 5, 0, 0)
 const earlier = new Date(2026, 0, 3, 14, 30)
 const html = render(h(Resume, { msg: { continuation_reason: 'restart', ts: today.getTime() } }))
 assert.match(html, /Resumed automatically · <time dateTime="[^"]+"[^>]*>09:05<\/time>/)
 const older = render(h(Resume, { msg: { continuation_reason: 'restart', ts: earlier.getTime() } }))
 assert.match(older, /3rd Jan 2026, 14:30<\/time>/, 'an earlier day keeps its date')
 assert.doesNotMatch(render(h(Resume, { msg: { continuation_reason: 'restart' } })), /<time/)
})
