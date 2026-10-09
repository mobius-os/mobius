/* Both active and saved surfaces use the same recorded within-response position. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderWithModels } from './modelRegistryRender.js'
import { createServer } from 'vite'
globalThis.window = { location: { origin: 'http://localhost', href: 'http://localhost/shell/' }, innerWidth: 420 }
const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
const { default: Active } = await vite.ssrLoadModule('/src/components/ChatView/AssistantReply.jsx')
const { assistantReplyGroups } = await vite.ssrLoadModule('/src/components/ChatView/assistantReplies.js')
const { default: Message } = await vite.ssrLoadModule('/src/components/ChatView/MsgContent.jsx')
const { PeerTimelineRows } = await vite.ssrLoadModule('/src/components/ChatView/PeerTimeline.jsx')
const { PeerTimelineContext } = await vite.ssrLoadModule('/src/components/ChatView/peerTimelineContext.js')
const { _resetDisclosureStateForTests, persistDisclosureOpen } = await vite.ssrLoadModule('/src/components/ChatView/disclosureState.js')
after(() => vite.close())
test('hidden final segment leaves a visible recovery control and stable source owner', () => {
  const first = { role: 'assistant', id: 'run', blocks: [
    { type: 'text', content: 'Progress saved.' },
    { type: 'error', message: 'Paused', resumable: true, pause: { kind: 'restart' } },
  ] }
  const hidden = { role: 'assistant', id: 'run:assistant:1', hidden: true, blocks: [] }
  const group = assistantReplyGroups([
    first,
    { role: 'user', hidden: true, steered: true, source_work_id: 'run' },
    hidden,
  ]).get(0)
  const html = render(Active, {
    replyGroup: group, activeRowIndex: -1, activeMirrorMsg: hidden,
    useDbActivePayload: true, chatId: 'chat', onResume: () => {},
    isLastMsg: group.lastVisibleIndex === 0,
  }, { tools: new Map(), positions: new Map() })
  assert.match(html, /class="chat__resume chat__recovery-action"/)
  assert.doesNotMatch(html, /data-key="run:assistant:1"/)
  assert.match(html, /data-key="run"/)
})
const note = { id: 'incoming', sender_chat_id: 'peer', sender_name: 'Colleague', body: 'New information', created_at: 2000, display_position: { assistant_message_id: 'answer', block_index: 0, text_offset: 9 } }
const context = { tools: new Map([['peer-incoming', [note]]]), positions: new Map([['answer', [note]]]) }
const message = { id: 'answer', role: 'assistant', blocks: [{ type: 'text', content: 'Earlier\n\nLater response' }] }
const replyGroup = assistantReplyGroups([message]).get(0)
function render(Component, props, value = context) {
  return renderWithModels(React.createElement(PeerTimelineContext.Provider, { value }, React.createElement(Component, props)))
}
test('live and reopened response place the incoming row before later prose', () => {
  const saved = render(Message, { msg: message, chatId: 'chat', messageKey: 'answer' })
  const live = render(Active, { replyGroup, activeMirrorMsg: message, activitySourceBlocks: message.blocks, useDbActivePayload: true, hasLivePayload: false, streamItems: [], chatId: 'chat', isStreaming: true })
  const streamed = render(Active, { replyGroup, activeMirrorMsg: null, useDbActivePayload: false, hasLivePayload: true, streamItems: message.blocks, chatId: 'chat', isStreaming: true })
  for (const html of [saved, live, streamed]) {
    assert.ok(html.indexOf('Earlier') < html.indexOf('Received from Colleague'))
    assert.ok(html.indexOf('Received from Colleague') < html.indexOf('Later response'))
    assert.equal(html.split('aria-label="Received from Colleague"').length, 2)
  }
})

test('a completed Goal splits activity at completion before later prose', () => {
  const msg = { id: 'goal-answer', role: 'assistant', blocks: [
    { type: 'text', content: 'Before completion' },
    { type: 'tool', tool: 'mobius_control:update_goal', input: '{"complete":"Verified"}', tool_use_id: 'complete', status: 'done', output_exit_code: 0 },
    { type: 'goal_history', summary: { id: 'goal', objective: 'Ship exactly once', status: 'completed' } },
    { type: 'tool', tool: 'Bash', tool_use_id: 'after', status: 'done' },
    { type: 'text', content: 'Later prose' },
  ] }
  const html = render(Message, { msg, chatId: 'chat', messageKey: msg.id }, { tools: new Map(), positions: new Map() })
  const card = html.indexOf('aria-label="Completed goal: Ship exactly once"')
  assert.ok(html.indexOf('Before completion') < card)
  assert.ok(card < html.indexOf('Later prose'))
  assert.equal(html.split('aria-label="Completed goal: Ship exactly once"').length, 2)
})

test('empty active payload still displays anchored activity exactly once', () => {
  const html = render(Message, { msg: { ...message, blocks: [] }, chatId: 'chat', messageKey: 'answer' })
  assert.equal(html.split('aria-label="Received from Colleague"').length, 2)
})

test('a helper row renders once at its recorded position', () => {
  const result = {
    id: 'delegation:review:completed', activityId: 'delegation:review:completed',
    type: 'helper_result', status: 'completed', task_key: 'Review',
    body: 'Reviewed outcome', consumption: 'available', created_at: 2000,
    display_position: { assistant_message_id: 'answer', block_index: 0, text_offset: 9 },
  }
  const helperContext = {
    tools: new Map(), positions: new Map([['answer', [result]]]),
  }
  const html = render(
    Message,
    { msg: message, chatId: 'chat', messageKey: 'answer' },
    helperContext,
  )
  const row = html.indexOf('chat__helper-row')
  assert.ok(html.indexOf('Earlier') < row)
  assert.ok(row < html.indexOf('Later response'))
  assert.equal(html.split('chat__helper-row').length, 2, 'the helper row renders once')
  assert.match(html, /Review/)
})

test('multiple helper rows join one surrounding activity disclosure', () => {
  const blocks = [
    { type: 'tool', tool: 'Bash', tool_use_id: 'command', status: 'done' },
    { type: 'thinking', thinking_id: 'thought', content: 'Reviewing', duration_ms: 1000 },
    { type: 'tool', tool: 'Edit', tool_use_id: 'edit', status: 'done' },
  ]
  const helper = (id, task, blockIndex) => ({
    id: `delegation:${id}:completed`, activityId: `delegation:${id}:completed`,
    type: 'helper_result', status: 'completed', task_key: task,
    body: `${task} outcome`, consumption: 'incorporated', created_at: 2000,
    display_position: { assistant_message_id: 'activity-answer', block_index: blockIndex },
  })
  const results = [helper('first', 'First review', 1), helper('second', 'Second review', 2)]
  _resetDisclosureStateForTests()
  persistDisclosureOpen('chat', 'activity-answer:activity:command', true)
  const html = render(Message, {
    msg: { id: 'activity-answer', role: 'assistant', blocks },
    chatId: 'chat', messageKey: 'activity-answer',
  }, {
    tools: new Map(), positions: new Map([['activity-answer', results]]),
  })

  assert.equal((html.match(/class="chat__activity chat__activity--done/g) || []).length, 1)
  assert.match(html, /Ran a command, exchanged messages, edited code/)
  assert.match(html, /First review/)
  assert.match(html, /Second review/)
  assert.equal((html.match(/chat__helper-row/g) || []).length, 2)
})

test('later peer messages share one high-level exchange and list each message inside', () => {
  const notes = [
    { ...note, id: 'one', sender_name: 'Review agent', body: 'First finding', type: 'peer_message' },
    { ...note, id: 'two', sender_name: 'Build agent', body: 'Second finding', type: 'peer_message' },
  ]
  _resetDisclosureStateForTests()
  persistDisclosureOpen('chat', 'peer-messages:one,two:activity:peer-one', true)
  persistDisclosureOpen('chat', 'peer-messages:one,two:tool:peer-one', true)
  persistDisclosureOpen('chat', 'peer-messages:one,two:tool:peer-two', true)
  const html = render(PeerTimelineRows, { notes, chatId: 'chat' }, {
    tools: new Map(), positions: new Map(),
  })
  assert.equal((html.match(/class="chat__activity chat__activity--done/g) || []).length, 1)
  assert.match(html, /Exchanged messages/)
  assert.match(html, /Received from Review agent/)
  assert.match(html, /Received from Build agent/)
  assert.match(html, /First finding/)
  assert.match(html, /Second finding/)
})

test('a cached compact Restart tool stays hidden without undercounting steps', () => {
  const html = render(Message, {
    msg: {
      id: 'restart-activity', role: 'assistant', blocks: [
        {
          type: 'activity', activity_id: '0:0:8', message_index: 0,
          start: 0, end: 8, tool_count: 7,
          entries: [
            { idx: 0, item: { type: 'tool', tool: 'Bash', status: 'done' } },
            { idx: 1, item: { type: 'tool', tool: 'mcp__mobius_control__request_restart', status: 'done' } },
          ],
        },
        {
          type: 'question', question_id: 'restart-card',
          questions: [{ id: 'restart', question: 'Restart?', options: [] }],
          platform_action: { type: 'restart', version: 2 },
        },
      ],
    },
    chatId: 'chat', messageKey: 'restart-activity',
  }, { tools: new Map(), positions: new Map() })

  assert.match(html, /\(6 steps\)/)
  assert.doesNotMatch(html, /request_restart/)
})

test('rendering retains a failed Restart attempt before the card-owned request', () => {
  const html = render(Message, {
    msg: {
      id: 'restart-retry', role: 'assistant', blocks: [
        {
          type: 'tool', tool: 'mcp__mobius_control__request_restart',
          tool_use_id: 'failed', status: 'done', output_exit_code: 1,
          output: 'Working tree is dirty',
        },
        {
          type: 'tool', tool: 'mcp__mobius_control__request_restart',
          tool_use_id: 'successful', status: 'done', output: '',
        },
        {
          type: 'question', question_id: 'restart-card',
          questions: [{ id: 'restart', question: 'Restart?', options: [] }],
          platform_action: { type: 'restart', version: 2 },
        },
      ],
    },
    chatId: 'chat', messageKey: 'restart-retry',
  }, { tools: new Map(), positions: new Map() })

  assert.match(html, /chat__tool--failed/)
  assert.match(html, /Asked to restart Möbius/)
  assert.match(html, /Restart\?/)
})

test('rendering a compact server projection retains its surviving failed Restart', () => {
  const html = render(Message, {
    msg: {
      id: 'restart-projected', role: 'assistant',
      interaction_tool_projection_version: 1,
      blocks: [
        {
          type: 'activity', activity_id: '0:0:1', message_index: 0,
          start: 0, end: 1, tool_count: 1,
          entries: [{
            idx: 0, item: {
              type: 'tool', tool: 'mcp__mobius_control__request_restart',
              tool_use_id: 'failed', status: 'done', output_exit_code: 1,
              output: 'Working tree is dirty',
            },
          }],
        },
        {
          type: 'question', question_id: 'restart-card',
          questions: [{ id: 'restart', question: 'Restart?', options: [] }],
          platform_action: { type: 'restart', version: 2 },
        },
      ],
    },
    chatId: 'chat', messageKey: 'restart-projected',
  }, { tools: new Map(), positions: new Map() })

  assert.match(html, /Asked to restart Möbius/)
  assert.match(html, /\(1 step\)/)
  assert.match(html, /Restart\?/)
})

test('a legacy compact projection repairs one sampled-out Restart request', () => {
  const html = render(Message, {
    msg: {
      id: 'legacy-restart-projected', role: 'assistant', blocks: [
        {
          type: 'activity', activity_id: '0:0:3', message_index: 0,
          start: 0, end: 3, tool_count: 3,
          entries: [
            {
              idx: 0, item: {
                type: 'tool', tool: 'mcp__mobius_control__request_restart',
                tool_use_id: 'failed-one', status: 'done', output_exit_code: 1,
                output: 'First failure',
              },
            },
            {
              idx: 1, item: {
                type: 'tool', tool: 'mcp__mobius_control__request_restart',
                tool_use_id: 'failed-two', status: 'done', output_exit_code: 1,
                output: 'Second failure',
              },
            },
          ],
        },
        { type: 'text', content: 'Ready after retry.' },
        {
          type: 'question', question_id: 'restart-card',
          questions: [{ id: 'restart', question: 'Restart?', options: [] }],
          platform_action: { type: 'restart', version: 2 },
        },
      ],
    },
    chatId: 'chat', messageKey: 'legacy-restart-projected',
  }, { tools: new Map(), positions: new Map() })

  assert.match(html, /\(2 steps\)/)
  assert.equal((html.match(/Asked to restart Möbius/g) || []).length, 2)
})

test('a marked compact projection trusts the corrected server count', () => {
  const html = render(Message, {
    msg: {
      id: 'fresh-restart-projected', role: 'assistant',
      interaction_tool_projection_version: 1,
      blocks: [
        {
          type: 'activity', activity_id: '0:0:3', message_index: 0,
          start: 0, end: 3, tool_count: 2,
          entries: [
            {
              idx: 0, item: {
                type: 'tool', tool: 'mcp__mobius_control__request_restart',
                tool_use_id: 'failed-one', status: 'done', output_exit_code: 1,
              },
            },
            {
              idx: 1, item: {
                type: 'tool', tool: 'mcp__mobius_control__request_restart',
                tool_use_id: 'failed-two', status: 'done', output_exit_code: 1,
              },
            },
          ],
        },
        {
          type: 'question', question_id: 'restart-card',
          questions: [{ id: 'restart', question: 'Restart?', options: [] }],
          platform_action: { type: 'restart', version: 2 },
        },
      ],
    },
    chatId: 'chat', messageKey: 'fresh-restart-projected',
  }, { tools: new Map(), positions: new Map() })

  assert.match(html, /\(2 steps\)/)
})

test('a later standalone Restart request owns the card before legacy activity', () => {
  const html = render(Message, {
    msg: {
      id: 'standalone-restart-owner', role: 'assistant', blocks: [
        {
          type: 'activity', activity_id: '0:0:3', message_index: 0,
          start: 0, end: 3, tool_count: 3,
          entries: [{
            idx: 0, item: {
              type: 'tool', tool: 'mcp__mobius_control__request_restart',
              tool_use_id: 'old-failed', status: 'done', output_exit_code: 1,
            },
          }],
        },
        { type: 'text', content: 'Trying once more.' },
        {
          type: 'tool', tool: 'mcp__mobius_control__request_restart',
          tool_use_id: 'successful', status: 'done', output: '',
          input: { marker: 'standalone-success' },
        },
        {
          type: 'question', question_id: 'restart-card',
          questions: [{ id: 'restart', question: 'Restart?', options: [] }],
          platform_action: { type: 'restart', version: 2 },
        },
      ],
    },
    chatId: 'chat', messageKey: 'standalone-restart-owner',
  }, { tools: new Map(), positions: new Map() })

  assert.match(html, /\(3 steps\)/)
  assert.doesNotMatch(html, /standalone-success/)
})


for (const isStreaming of [true, false]) {
  test(`hidden replay uses one Markdown paragraph in the shared reply surface (${isStreaming})`, () => {
    const prefix = 'This sentence continues;'
    const answer = `${prefix} without an artificial paragraph boundary.`
    const first = { role: 'assistant', id: 'reply', blocks: [{ type: 'text', content: prefix }], source_ref: { message_index: 1, count: 1 } }
    const tail = { role: 'assistant', id: 'reply:assistant:1', blocks: [{ type: 'text', content: answer }], source_ref: { message_index: 3, count: 1 } }
    const group = assistantReplyGroups([first, { role: 'user', hidden: true, steered: true }, tail]).get(0)
    const html = render(Active, {
      replyGroup: group, activeRowIndex: isStreaming ? 1 : -1,
      activeMirrorMsg: tail, activitySourceBlocks: tail.blocks,
      useDbActivePayload: true, isStreaming, chatId: 'reply-fixture',
    }, { tools: new Map(), positions: new Map() })
    assert.ok(html.includes(answer), 'the suffix belongs to the original paragraph')
    assert.equal((html.match(/<p\b[^>]*>/g) || []).length, 1)
    assert.equal((html.match(/<section class="chat__sources"/g) || []).length, isStreaming ? 0 : 1)
    assert.match(html, /data-key="reply"/)
    assert.match(html, /data-key="reply:assistant:1"/)
  })
}


test('one reply owns its final References, including a folded source row', () => {
  const first = { role: 'assistant', id: 'reference-reply', blocks: [{ type: 'text', content: 'First section' }], source_ref: { message_index: 0, count: 1 } }
  const tail = { role: 'assistant', id: 'reference-reply:assistant:1', blocks: [
    { type: 'thinking', thinking_id: 'thinking', content: 'Considering the new information' },
    { type: 'text', content: 'First section continued after thinking' },
  ] }
  const folded = { role: 'assistant', id: 'reference-reply:assistant:2', hidden: true,
    blocks: [{ type: 'tool', sources: [{ url: 'https://folded.example', title: 'Folded source' }] }],
    source_ref: { message_index: 4, count: 1 } }
  const carrier = { role: 'user', steered: true, hidden: true }
  const group = assistantReplyGroups([first, carrier, tail, carrier, folded]).get(0)
  _resetDisclosureStateForTests()
  persistDisclosureOpen('reply-fixture', `${tail.id}:references`, true)
  const html = render(Active, { replyGroup: group, activeRowIndex: -1,
    activeMirrorMsg: folded, useDbActivePayload: true, chatId: 'reply-fixture' },
  { tools: new Map(), positions: new Map() })
  assert.equal((html.match(/class="chat__reply"/g) || []).length, 1)
  assert.equal((html.match(/class="chat__sources(?: |")/g) || []).length, 1)
  assert.ok(html.indexOf('First section') < html.indexOf('chat__activity'))
  assert.ok(html.indexOf('chat__activity') < html.indexOf('continued after thinking'))
  assert.ok(html.indexOf('continued after thinking') < html.indexOf('chat__sources'))
  assert.match(html, /href="https:\/\/folded.example"/)
  assert.match(html, /data-key="reference-reply"/)
  assert.match(html, /data-key="reference-reply:assistant:1"/)
  assert.doesNotMatch(html, /data-key="reference-reply:assistant:2"/)
})


test('grouped replies carry authoritative manual recovery to their existing action', () => {
  for (const kind of ['memory', 'storage', 'model_capacity']) {
    const msg = { id: 'recover-run', role: 'assistant', blocks: [
      { type: 'error', resumable: true, pause: { kind } },
    ] }
    const props = {
      replyGroup: assistantReplyGroups([msg]).get(0),
      activeMirrorMsg: msg, useDbActivePayload: true, onResume() {}, isLastMsg: true,
    }
    const manual = render(Active, { ...props, handoff: { kind: 'recovery' } })
    assert.match(manual, /class="chat__resume chat__recovery-action"/)
    const automatic = render(Active, { ...props, handoff: { kind: 'automatic' } })
    assert.doesNotMatch(automatic, /class="chat__resume chat__recovery-action"/)
    assert.doesNotMatch(manual, /will continue automatically|Trying again shortly/)
  }
})

test('live tools joined into an earlier row stay inside the marked current response', () => {
  const tool = (id, status = 'done') => ({ type: 'tool', tool: 'Bash', input: `echo ${id}`, tool_use_id: id, status })
  const first = { role: 'assistant', id: 'rt-join:assistant:1', blocks: [tool('inspect')], ts: 2 }
  const next = { role: 'assistant', id: 'rt-join:assistant:2', blocks: [], ts: 4 }
  const group = assistantReplyGroups([
    first,
    { role: 'user', hidden: true, steered: true, kind: 'delegation_result', source_work_id: 'goal-root', ts: 3 },
    next,
  ]).get(0)
  assert.equal(group.rows.length, 2)
  const props = { replyGroup: group, activeMirrorMsg: next, activitySourceBlocks: [], useDbActivePayload: false,
    hasLivePayload: true, streamItems: [tool('continue', 'running')], chatId: 'chat', isStreaming: true }
  const html = render(Active, { ...props, activeRowIndex: 1 }, { tools: new Map(), positions: new Map() })
  const reply = html.indexOf('data-current-response="true"')
  const earlierRow = html.indexOf('data-key="rt-join:assistant:1"')
  assert.ok(reply >= 0 && reply < earlierRow, 'the active reply as a whole is the current response')
  assert.doesNotMatch(html.slice(earlierRow, html.indexOf('>', earlierRow)), /data-active-assistant/)
  const live = html.indexOf('in progress"')
  assert.ok(live > earlierRow && live < html.indexOf('data-active-assistant="true"'),
    'the live activity header is presented in the earlier, unmarked row')
  const settled = render(Active, { ...props, activeRowIndex: -1, useDbActivePayload: true, hasLivePayload: false, isStreaming: false },
    { tools: new Map(), positions: new Map() })
  assert.doesNotMatch(settled, /data-current-response/)
})
