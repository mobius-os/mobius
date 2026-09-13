/* Both active and saved surfaces use the same recorded within-response position. */
import test, { after } from 'node:test'
import assert from 'node:assert/strict'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { QueryClient, QueryObserver } from '@tanstack/react-query'
import { createServer } from 'vite'
globalThis.window = { location: { origin: 'http://localhost', href: 'http://localhost/shell/' }, innerWidth: 420 }
const vite = await createServer({ appType: 'custom', logLevel: 'error', server: { middlewareMode: true, hmr: false, ws: false }, ssr: { noExternal: ['@openai/apps-sdk-ui'] } })
const { default: Active } = await vite.ssrLoadModule('/src/components/ChatView/ActiveAssistantSurface.jsx')
const { default: Message } = await vite.ssrLoadModule('/src/components/ChatView/MsgContent.jsx')
const { PeerTimelineLoadError } = await vite.ssrLoadModule('/src/components/ChatView/PeerTimeline.jsx')
const { PeerTimelineContext } = await vite.ssrLoadModule('/src/components/ChatView/peerTimelineContext.js')
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

test('activity load errors stay quiet while restart recovery is active', () => {
  assert.equal(renderToStaticMarkup(React.createElement(
    PeerTimelineLoadError, { error: false, onRetry: () => {} },
  )), '')
  assert.equal(renderToStaticMarkup(React.createElement(
    PeerTimelineLoadError, {
      error: true, recoveryActive: true, onRetry: () => {},
    },
  )), '')
})

test('a settled activity load failure retains a manual retry', () => {
  const html = renderToStaticMarkup(React.createElement(
    PeerTimelineLoadError, {
      error: true, recoveryActive: false, onRetry: () => {},
    },
  ))
  assert.match(html, /role="status"/)
  assert.match(html, /Chat activity couldn’t refresh/)
  assert.match(html, /<button type="button">Try again<\/button>/)
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

test('a retained activity error stays quiet through reconnect refetch, then reports only a settled failure', async (t) => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  t.after(() => client.clear())
  const key = ['chat-activity', 'restart-transition']
  let outcome = 'success'
  let releaseRecovery
  const observer = new QueryObserver(client, {
    queryKey: key,
    retry: false,
    staleTime: Infinity,
    queryFn: async () => {
      if (outcome === 'failure') throw new Error('restart gap')
      if (outcome === 'recovering') {
        await new Promise(resolve => { releaseRecovery = resolve })
      }
      return { events: [], next_before: null }
    },
  })
  const unsubscribe = observer.subscribe(() => {})
  t.after(unsubscribe)

  await observer.refetch()
  outcome = 'failure'
  await client.invalidateQueries({ queryKey: key })
  assert.equal(observer.getCurrentResult().isError, true)

  outcome = 'recovering'
  const recovery = client.invalidateQueries({ queryKey: key })
  await new Promise(resolve => setImmediate(resolve))
  const recovering = observer.getCurrentResult()
  assert.equal(recovering.isError, true)
  assert.equal(recovering.isFetching, true)
  assert.equal(renderToStaticMarkup(React.createElement(
    PeerTimelineLoadError, {
      error: recovering.isError,
      recoveryActive: recovering.isFetching,
      onRetry: () => {},
    },
  )), '')

  releaseRecovery()
  await recovery
  assert.equal(observer.getCurrentResult().isError, false)

  outcome = 'failure'
  await client.invalidateQueries({ queryKey: key })
  const settledFailure = observer.getCurrentResult()
  assert.equal(settledFailure.isFetching, false)
  assert.match(renderToStaticMarkup(React.createElement(
    PeerTimelineLoadError, {
      error: settledFailure.isError,
      recoveryActive: settledFailure.isFetching,
      onRetry: () => {},
    },
  )), /Try again/)
})
