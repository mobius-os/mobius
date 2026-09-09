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
