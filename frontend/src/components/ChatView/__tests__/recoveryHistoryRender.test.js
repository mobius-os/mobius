/* The combined recovery notice, rendered through the saved-answer renderer from
   the same projection ChatView applies after the peer timeline. */
import { after, afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { click, h, mount, unmountAll } from './mountedRender.js'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'
import { TEST_MODEL_REGISTRY } from './modelRegistryRender.js'

const vite = await createSsrRenderServer()
after(() => vite.close())
const { default: Message } = await vite.ssrLoadModule('/src/components/ChatView/MsgContent.jsx')
const { combineRecoveryHistory, supersedeResumedPauseBlocks } = await vite.ssrLoadModule('/src/components/ChatView/chatRuntimeState.js')
const client = new QueryClient()
client.setQueryData(['models', 'registry'], TEST_MODEL_REGISTRY)
after(() => client.clear())
afterEach(unmountAll)

const paused = {
  id: 'paused', role: 'assistant', ts: Date.UTC(2026, 0, 1, 9), run_ended_ts: Date.UTC(2026, 0, 1, 10),
  blocks: [
    { type: 'text', content: 'Progress saved.' },
    { type: 'error', resumable: true, pause: { kind: 'restart' } },
  ],
}
const resumed = {
  id: 'resumed', role: 'assistant', continuation_reason: 'manual', ts: Date.UTC(2026, 0, 1, 11),
  blocks: [{ type: 'text', content: 'Continuing.' }],
}
const { projectChatActivity } = await vite.ssrLoadModule('/src/components/ChatView/chatActivity.js')
const { foldPeerActivity } = await vite.ssrLoadModule('/src/components/ChatView/peerTimeline.js')
// ChatView's order: settle pauses, project the peer timeline, then combine.
const project = (rows, events = []) => {
  const settled = supersedeResumedPauseBlocks(rows)
  const timeline = foldPeerActivity(settled, projectChatActivity(settled, events, 'chat'), 'chat')
  return combineRecoveryHistory(timeline.messages, timeline)
}
const show = msg => mount(h(Message, { msg, chatId: 'chat', messageKey: msg.id }), {
  wrap: node => h(QueryClientProvider, { client }, node),
})
const text = view => view.container.textContent

test('the pause moves into the resume notice, whose history opens with both times', () => {
  const [pauseRow, resumeRow] = project([paused, resumed])
  const before = show(pauseRow)
  assert.match(text(before), /Progress saved\./)
  assert.doesNotMatch(text(before), /Paused for a platform update/, 'the cause is not drawn twice')

  const after = show(resumeRow)
  assert.match(text(after), /Paused for a platform update/)
  assert.ok(text(after).indexOf('Paused for a platform update') < text(after).indexOf('Continuing.'),
    'the cause leads the resumed answer')
  const toggle = after.container.querySelector('button')
  assert.equal(toggle.getAttribute('aria-expanded'), 'false')
  click(toggle)
  const times = after.container.querySelector('.chat__recovery-events').querySelectorAll('time')
    .map(node => node.getAttribute('dateTime') ?? node.getAttribute('datetime'))
  assert.deepEqual(times, [new Date(paused.run_ended_ts).toISOString(), new Date(resumed.ts).toISOString()])
})

test('a helper result that arrived during the pause keeps the pause card above it', () => {
  const result = { id: 'helper:1', type: 'helper_result', created_at: new Date(Date.UTC(2026, 0, 1, 10, 30)).toISOString() }
  const [pauseRow, resumeRow] = project([paused, resumed], [result])
  assert.match(text(show(pauseRow)), /Paused for a platform update/)
  const after = text(show(resumeRow))
  assert.match(after, /Resumed manually/)
  assert.doesNotMatch(after, /Paused for a platform update/)
})
