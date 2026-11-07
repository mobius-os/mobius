/* The transcript, not the block, attests a passive block's chat and whether it
   is that chat's newest block of its app and intent. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { AppBlockChatContext, appBlockChatKey, createAppBlockChat, useAppBlockChat } from '../markdown/appBlockChat.js'
import { appBlockFromToken } from '../markdown/appBlock.js'

// Fake section nodes ordered by their position in the transcript.
const node = position => ({ position, compareDocumentPosition: other => other.position > position ? 4 : 2 })

test('only the last mounted block of an app and intent is latest, in document order', () => {
  const chat = createAppBlockChat('chat-1')
  const [older, newer, other] = [{}, {}, {}]
  chat.place('contribute\nchat:contributions', newer, node(5))
  chat.place('contribute\nchat:contributions', older, node(1))
  chat.place('contribute\nreview:x', other, node(3))
  assert.equal(chat.latest('contribute\nchat:contributions', older), false)
  assert.equal(chat.latest('contribute\nchat:contributions', newer), true)
  assert.equal(chat.latest('contribute\nreview:x', other), true, 'a different intent is its own group')
  assert.equal(chat.newestNode('contribute\nchat:contributions').position, 5)
})

test('a superseded block stays superseded while it is mounted, even if the newer one remounts or moves', () => {
  const chat = createAppBlockChat('chat-1')
  const key = 'contribute\nchat:contributions'
  const [older, newer] = [{}, {}]
  chat.place(key, older, node(1))
  assert.equal(chat.latest(key, older), true, 'alone, it is the newest')
  chat.place(key, newer, node(2))
  assert.equal(chat.latest(key, older), false)
  chat.remove(key, newer) // a streamed reply settling remounts its block
  assert.equal(chat.latest(key, older), false)
  chat.place(key, older, node(1)) // the older block swaps its static and live views
  assert.equal(chat.latest(key, older), false)
  chat.remove(key, older)
  chat.place(key, older, node(1)) // only a fresh mount starts over
  assert.equal(chat.latest(key, older), true)
})

test('subscribers hear placements and refresh hints; only known reasons are raised', () => {
  const chat = createAppBlockChat('chat-1')
  let heard = 0
  const stop = chat.subscribe(() => { heard += 1 })
  chat.place('a\nb:c', {}, node(1))
  chat.refresh('turn')
  chat.refresh('visible')
  chat.refresh('whatever')
  assert.deepEqual(chat.refreshSnapshot(), { seq: 2, reason: 'visible' })
  assert.equal(heard, 3)
  stop()
  chat.refresh('turn')
  assert.equal(heard, 3)
})

test('the block\'s own JSON cannot supply a chat context', () => {
  const block = appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({
    app: 'contribute', intent: 'chat:contributions', title: 'Cards', interaction: 'inline',
    context: { chatId: 'someone-elses-chat', latest: true }, chat_id: 'someone-elses-chat' }) })
  assert.equal('context' in block, false)
  assert.equal('chat_id' in block, false)
  assert.equal(appBlockChatKey(block), 'contribute\nchat:contributions')
  let seen = null
  const Probe = () => { seen = useAppBlockChat(appBlockChatKey(block)); return null }
  renderToStaticMarkup(createElement(Probe))
  assert.equal(seen.context, null, 'outside a chat transcript there is no context')
  renderToStaticMarkup(createElement(AppBlockChatContext.Provider, { value: createAppBlockChat('chat-1') }, createElement(Probe)))
  assert.deepEqual(seen.context, { chatId: 'chat-1', latest: true })
  assert.deepEqual(seen.refresh, { seq: 0, reason: '' })
})
