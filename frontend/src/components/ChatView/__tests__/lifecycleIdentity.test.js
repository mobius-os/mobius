/* Rendered lifecycle identities must remain distinct after the same outcome. */
import { after, test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement as h } from 'react'
import { renderToStaticMarkup as render } from 'react-dom/server'
import { createServer } from 'vite'
const previousWindow = globalThis.window
globalThis.window = { location: { href: 'http://localhost/' } }
after(() => { globalThis.window = previousWindow })
const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
after(() => vite.close())
const load = async name => (await vite.ssrLoadModule('/src/components/ChatView/' + name + '.jsx')).default
const Goal = await load('GoalHistoryCard')
const Wait = await load('WaitHistoryCard')
const Resume = await load('ContinuationCard')
const ErrorCard = await load('ErrorCard')
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

test('composer Waiting follows the compact Goal identity instead of a tile or badge', async () => {
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
