/* Both active and saved surfaces use the same recorded within-response position. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'
globalThis.window = { location: { origin: 'http://localhost', href: 'http://localhost/shell/' }, innerWidth: 420 }
const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
const { default: Active } = await vite.ssrLoadModule('/src/components/ChatView/ActiveAssistantSurface.jsx')
const { default: Message } = await vite.ssrLoadModule('/src/components/ChatView/MsgContent.jsx')
const { PeerTimelineRows } = await vite.ssrLoadModule('/src/components/ChatView/PeerTimeline.jsx')
const { PeerTimelineContext } = await vite.ssrLoadModule('/src/components/ChatView/peerTimelineContext.js')
const { _resetDisclosureStateForTests, persistDisclosureOpen } = await vite.ssrLoadModule('/src/components/ChatView/disclosureState.js')
after(() => vite.close())
const note = { id: 'incoming', sender_chat_id: 'peer', sender_name: 'Colleague', body: 'New information', created_at: 2000, display_position: { assistant_message_id: 'answer', block_index: 0, text_offset: 9 } }
const context = { tools: new Map([['peer-incoming', [note]]]), positions: new Map([['answer', [note]]]) }
const message = { id: 'answer', role: 'assistant', blocks: [{ type: 'text', content: 'Earlier\n\nLater response' }] }
function render(Component, props, value = context) {
  return renderToStaticMarkup(React.createElement(PeerTimelineContext.Provider, { value }, React.createElement(Component, props)))
}
test('live and reopened response place the incoming row before later prose', () => {
  const saved = render(Message, { msg: message, chatId: 'chat', messageKey: 'answer' })
  const live = render(Active, { activeMirrorMsg: message, activityMessageId: 'answer', activitySourceBlocks: message.blocks, useDbActivePayload: true, hasLivePayload: false, streamItems: [], chatId: 'chat', dataKey: 'answer', isStreaming: true })
  const streamed = render(Active, { activeMirrorMsg: null, activityMessageId: 'answer', useDbActivePayload: false, hasLivePayload: true, streamItems: message.blocks, chatId: 'chat', dataKey: 'answer', isStreaming: true })
  for (const html of [saved, live, streamed]) {
    assert.ok(html.indexOf('Earlier') < html.indexOf('Received from Colleague'))
    assert.ok(html.indexOf('Received from Colleague') < html.indexOf('Later response'))
    assert.equal(html.split('aria-label="Received from Colleague"').length, 2)
  }
})

test('empty active payload still displays anchored activity exactly once', () => {
  const html = render(Message, { msg: { ...message, blocks: [] }, chatId: 'chat', messageKey: 'answer' })
  assert.equal(html.split('aria-label="Received from Colleague"').length, 2)
})

test('busy-parent helper result renders once at the same recorded frontier', () => {
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
  assert.ok(html.indexOf('Earlier') < html.indexOf('Helper finished · Review'))
  assert.ok(html.indexOf('Helper finished · Review') < html.indexOf('Later response'))
  assert.equal(html.split('aria-label="Helper finished · Review"').length, 2)
})

test('multiple helper completions join one surrounding activity disclosure', () => {
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
  assert.match(html, /Helper finished · First review/)
  assert.match(html, /Helper finished · Second review/)
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
  assert.match(html, /mcp__mobius_control__request_restart/)
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

  assert.match(html, /mcp__mobius_control__request_restart/)
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
  assert.equal((html.match(/mcp__mobius_control__request_restart/g) || []).length, 2)
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
