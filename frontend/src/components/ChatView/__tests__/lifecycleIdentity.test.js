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
 for (const html of [goal, wait]) assert.match(html, /class="chat__lifecycle-outcome"/)
})
test('Waiting preserves its clock across completed, attention and stopped states', () => {
 const states = ['met', 'expired', 'failed', 'cancelled'].map(status => render(h(Wait, { summary: { description: 'Deployment ready', status } })))
 for (const html of states) assert.equal(icon(html), icon(states[0]))
})
test('resume, pause and errors share icon geometry without sharing meaning', () => {
 const resumed = render(h(Resume, { msg: { continuation_reason: 'manual' } }))
 const paused = render(h(ErrorCard, { block: { pause: { kind: 'restart' }, resumable: true } }))
 const failed = render(h(ErrorCard, { block: { message: 'Unavailable' } }))
 assert.ok(icon(resumed)); assert.ok(icon(paused)); assert.ok(icon(failed))
 assert.notEqual(icon(paused), icon(failed))
 assert.match(resumed, /Resumed manually/); assert.match(paused, /Paused/); assert.match(failed, /role="alert"/)
})

test('composer identity stays outside truncated text and disclosure remains explicit', async () => {
 const Rail = await load('ProgressRail')
 const Identity = await load('LifecycleIcon')
 const Draft = await load('GoalDraftChip')
 const rail = render(h(Rail, { items: [{
   key: 'sample', label: 'A very long objective', expandable: true,
   icon: h(Identity, { kind: 'goal' }),
 }], ariaLabel: 'Progress' }))
 assert.ok(rail.indexOf('chat__lifecycle-icon') < rail.indexOf('chat__progress-step-label'))
 assert.match(rail, /aria-expanded="false"/)
 assert.match(rail, /chat__progress-chevron/)
 assert.ok(icon(render(h(Draft, { objective: 'Draft objective' }))))
 const { WaitCard } = await vite.ssrLoadModule('/src/components/ChatView/WaitingChip.jsx')
 const wait = render(h(WaitCard, {
   wait: { id: 'sample', kind: 'condition', description: 'Review approved' },
   expanded: false, onToggle: () => {}, onCancel: () => {},
 }))
 assert.ok(icon(wait))
 assert.ok(wait.indexOf('chat__lifecycle-icon') < wait.indexOf('chat__wait-text'))
 assert.match(wait, /aria-expanded="false"/)
})
