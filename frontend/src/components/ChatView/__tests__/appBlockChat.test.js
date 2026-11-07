/* The transcript, not the block, attests a passive block's chat and whether it
   is that chat's newest block of its app and intent. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import {
  ASK_LIMIT, ASK_REPEAT_MS, ASK_WINDOW_MS, AppBlockChatContext, appBlockChatKey, createAppBlockChat, useAppBlockChat,
} from '../markdown/appBlockChat.js'
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

// ---- Card asks (ask-v1).
function askChat(results = []) {
  const sends = []
  let clock = 1_000_000
  const chat = createAppBlockChat('chat-1', { now: () => clock,
    send: async (message, options) => { sends.push({ message, ...options }); const next = results.shift(); if (next instanceof Error) throw next; return next ?? true } })
  return { chat, sends, tick: ms => { clock += ms } }
}
const KEY = 'contribute\nchat:contributions'

test('one tap sends one owner message; the card waits until a turn settles', async () => {
  const { chat, sends } = askChat()
  const card = {}
  chat.place(KEY, card, node(1))
  const first = chat.ask(KEY, card, 'Fix it')
  assert.equal(chat.askState(KEY, 'Fix it'), 'sending')
  assert.equal(await chat.ask(KEY, card, 'Fix it'), false, 'a second tap while sending sends nothing')
  assert.equal(await first, true)
  assert.equal(chat.askState(KEY, 'Fix it'), 'asked')
  assert.equal(await chat.ask(KEY, card, 'Another reason'), false, 'one ask per card until the turn settles')
  chat.turnSettled()
  assert.equal(chat.askState(KEY, 'Fix it'), 'asked', 'the same message is not sent again soon')
  assert.equal(chat.askState(KEY, 'Another reason'), 'ready')
  assert.deepEqual(sends.map(send => send.message), ['Fix it'])
})

test('only the newest card of its key may ask', async () => {
  const { chat, sends } = askChat()
  const [older, newer] = [{}, {}]
  chat.place(KEY, older, node(1))
  chat.place(KEY, newer, node(2))
  assert.equal(await chat.ask(KEY, older, 'Fix it'), false)
  assert.equal(await chat.ask(KEY, newer, 'Fix it'), true)
  assert.equal(sends.length, 1)
  assert.equal(await createAppBlockChat('chat-1').ask(KEY, {}, 'No sender'), false)
})

test('the same message waits ten minutes, and a chat takes six asks an hour', async () => {
  const { chat, tick } = askChat()
  const card = {}
  chat.place(KEY, card, node(1))
  await chat.ask(KEY, card, 'Fix it')
  chat.turnSettled()
  tick(ASK_REPEAT_MS - 1)
  assert.equal(chat.askState(KEY, 'Fix it'), 'asked')
  tick(1)
  assert.equal(chat.askState(KEY, 'Fix it'), 'ready')
  for (let i = 1; i < ASK_LIMIT; i++) { assert.equal(await chat.ask(KEY, card, `Reason ${i}`), true); chat.turnSettled() }
  assert.equal(chat.askState(KEY, 'Reason 99'), 'limited')
  assert.equal(await chat.ask(KEY, card, 'Reason 99'), false)
  tick(ASK_WINDOW_MS)
  assert.equal(chat.askState(KEY, 'Reason 99'), 'ready')
})

test('a failed send frees the card and a retry reuses its identity, so a send that did arrive is not doubled', async () => {
  const { chat, sends } = askChat([false, new Error('offline'), true])
  const card = {}
  chat.place(KEY, card, node(1))
  assert.equal(await chat.ask(KEY, card, 'Fix it'), false)
  assert.equal(chat.askState(KEY, 'Fix it'), 'ready')
  assert.equal(await chat.ask(KEY, card, 'Fix it'), false)
  assert.equal(await chat.ask(KEY, card, 'Fix it'), true)
  assert.equal(new Set(sends.map(send => send.cid)).size, 1)
  assert.ok(sends[0].cid)
  chat.turnSettled()
  assert.equal(await chat.ask(KEY, card, 'Other'), true)
  assert.notEqual(sends.at(-1).cid, sends[0].cid, 'another message is another identity')
})

test('the block hook exposes its own card ask', async () => {
  const { chat, sends } = askChat()
  let seen = null
  const Probe = () => { seen = useAppBlockChat(KEY); return null }
  renderToStaticMarkup(createElement(AppBlockChatContext.Provider, { value: chat }, createElement(Probe)))
  assert.equal(seen.askState('Fix it'), 'ready')
  renderToStaticMarkup(createElement(Probe))
  assert.equal(seen.askState('Fix it'), 'limited', 'outside a chat nothing can be asked')
  assert.equal(await seen.ask('Fix it'), false)
  assert.equal(sends.length, 0)
})

test('the chat sends a card ask as an owner message that leaves the composer\'s draft alone', async () => {
  const { readFileSync } = await import('node:fs')
  const src = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  assert.match(src, /askSendRef\.current = \(text, \{ cid \}\) => doSend\(text, \{ attachments: \[\], composer: false, cid \}\)/)
  assert.match(src, /createAppBlockChat\(chatId, \{ send: \(text, options\) => askSendRef\.current\(text, options\) \}\)/)
  assert.match(src, /appBlockChat\?\.turnSettled\(\)\n\s+if \(!hidden && document\.visibilityState === 'visible'\) appBlockChat\?\.refresh\('turn'\)/,
    'every settled turn ends an answered ask, visible or not')
  const doSend = src.slice(src.indexOf('const doSend = useCallback('), src.indexOf('const doSendSilent = useCallback('))
  // Every composer mutation in doSend goes through a guarded helper.
  assert.equal((doSend.match(/setComposerInput\(''\)/g) || []).length, 1)
  assert.match(doSend, /function clearComposerForSend\(\) \{\n\s+if \(!fromComposer\) return\n\s+setComposerInput\(''\)/)
  assert.match(doSend, /function keepFailedDraft\(failedAttempt\) \{\n\s+if \(!fromComposer\) return/)
  assert.match(doSend, /if \(fromComposer && listeningRef\.current\) stopVoiceRef\.current\?\.\(\)/)
  assert.match(doSend, /if \(fromComposer && shouldDismissComposerKeyboardOnSubmit\(/)
  assert.equal((doSend.match(/rememberFailedAttempt\(|restoreComposerText\(|settleAmbiguousSendConfirmation\(/g) || []).length, 3,
    'only inside the guarded helpers')
})
