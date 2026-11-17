/* An answer that settles and then continues (a question answered inside the
   same row) keeps its rendered prose: switching between saved and live states
   only toggles the streaming cursor, never the markdown tree under it. */
import { after, afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { h, mount, unmountAll } from './mountedRender.js'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
after(() => vite.close())
const { default: MsgContent } = await vite.ssrLoadModule('/src/components/ChatView/MsgContent.jsx')

afterEach(unmountAll)

const blockMessage = {
  role: 'assistant',
  id: 'reply-blocks',
  blocks: [{ type: 'text', content: 'First paragraph of the answer.' }],
}
const plainMessage = { role: 'assistant', id: 'reply-plain', content: 'A reply saved without blocks.' }

const props = (msg, state) => ({
  msg,
  chatId: 'chat-1',
  messageKey: msg.id,
  isLastMsg: true,
  liveQuestionId: null,
  ...state,
})
const settled = { isActiveAnswer: false, isStreaming: false }
const active = { isActiveAnswer: true, isStreaming: false }
const streaming = { isActiveAnswer: true, isStreaming: true }

for (const [label, msg] of [['block', blockMessage], ['plain-content', plainMessage]]) {
  test(`a ${label} answer keeps its paragraph node from settled through streaming and back`, () => {
    const view = mount(h(MsgContent, props(msg, settled)))
    const paragraph = view.container.querySelector('p')
    assert.ok(paragraph, 'the answer renders a paragraph')
    // Identity is compared as a boolean: a failed node comparison would print
    // the whole fiber-linked fake DOM.
    const cursor = () => view.container.querySelectorAll('.chat__cursor').length
    assert.equal(cursor(), 0, 'a settled answer has no cursor')

    view.rerender(h(MsgContent, props(msg, active)))
    assert.ok(view.container.querySelector('p') === paragraph, 'becoming active keeps the paragraph')
    assert.equal(cursor(), 0, 'an active answer that is not streaming has no cursor')

    view.rerender(h(MsgContent, props(msg, streaming)))
    assert.ok(view.container.querySelector('p') === paragraph, 'streaming keeps the paragraph')
    assert.equal(cursor(), 1, 'the cursor shows only while streaming')

    view.rerender(h(MsgContent, props(msg, settled)))
    assert.ok(view.container.querySelector('p') === paragraph, 'settling again keeps the paragraph')
    assert.equal(cursor(), 0, 'the cursor leaves when the answer settles')
  })
}
