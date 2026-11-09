/* The transcript, not the block, attests a passive block's chat and whether it
   is that chat's newest block of its app and intent. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import {
  AppBlockChatContext, appBlockChatKey, askCid, blockFingerprint, createAppBlockChat, useAppBlockChat,
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
const KEY = 'contribute\nchat:contributions'
const REFS = [{ id: 'b-1', code: 'needs_review', rev: 'b'.repeat(40) }, { id: 'a-1', code: 'upstream_conflict', rev: 'a'.repeat(40) }]
const FINGERPRINT = blockFingerprint(appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify({
  app: 'contribute', intent: 'chat:contributions', title: 'Cards', interaction: 'inline', items: [{ title: 'A', intent: 'review:a-1' }],
  action: { label: 'Contribute', intent: 'chat-send:a-1' } }) }))
const request = { message: 'Please fix', fingerprint: FINGERPRINT, refs: REFS }
// The server's existing duplicate check, as doSend reports it: a repeated
// client id is the same message, posted once, and answered 'duplicate'.
function server() {
  const posted = new Map()
  return { posted, send: async (message, { cid }) => { if (posted.has(cid)) return 'duplicate'; posted.set(cid, message); return true } }
}
const askChat = send => createAppBlockChat('chat-1', { send })

test('the ask id depends only on the chat, the block content and the refs with their revisions', async () => {
  const id = await askCid('chat-1', FINGERPRINT, REFS)
  assert.match(id, /^ask-[0-9a-f]{32}$/)
  assert.equal(await askCid('chat-1', FINGERPRINT, [...REFS].reverse()), id, 'order does not matter')
  for (const other of [await askCid('chat-2', FINGERPRINT, REFS), await askCid('chat-1', `${FINGERPRINT}x`, REFS),
    await askCid('chat-1', FINGERPRINT, REFS.slice(1)), await askCid('chat-1', FINGERPRINT, [REFS[0], { ...REFS[1], rev: 'c'.repeat(40) }])]) {
    assert.notEqual(other, id)
  }
})

test('a lost acknowledgement retried later, on another device with another clock, posts once', async () => {
  const { posted, send } = server()
  let lose = true
  const flaky = async (message, options) => { const result = await send(message, options); if (lose) { lose = false; throw new Error('lost ack') } return result }
  const realNow = Date.now
  const card = {}
  try {
    Date.now = () => Date.parse('2026-11-07T10:59:59.999Z')
    const first = askChat(flaky)
    first.place(KEY, card, node(1))
    assert.equal(await first.ask(KEY, card, request), false, 'the server took it, but the answer was lost')
    assert.equal(first.askState(request), 'ready')
    Date.now = () => Date.parse('2026-11-07T11:00:00.000Z')
    assert.equal(await first.ask(KEY, card, request), true, 'the retry is recognised')
    assert.equal(first.askState(request), 'already', 'and said truthfully')
    Date.now = () => Date.parse('2026-11-08T03:17:00.000Z') // another device, skewed clock
    const other = askChat(send)
    other.place(KEY, card, node(1))
    assert.equal(await other.ask(KEY, card, request), true)
    assert.equal(other.askState(request), 'already')
  } finally { Date.now = realNow }
  assert.equal(posted.size, 1)
})

test('reloads, tabs and concurrent taps of one state post once; a new revision can be asked', async () => {
  const { posted, send } = server()
  const views = [askChat(send), askChat(send), askChat(send)] // three views of one chat
  const card = {}
  for (const chat of views) chat.place(KEY, card, node(1))
  const results = await Promise.all([...views.map(chat => chat.ask(KEY, card, request)), views[0].ask(KEY, card, request)])
  assert.deepEqual(results, [true, true, true, false], 'the same view refuses a second tap while sending')
  assert.equal(posted.size, 1)
  assert.deepEqual(views.map(chat => chat.askState(request)).sort(), ['already', 'already', 'asked'])
  views[0].turnSettled()
  assert.equal(views[0].askState(request), 'ready')
  assert.equal(await views[0].ask(KEY, card, request), true)
  assert.equal(views[0].askState(request), 'already', 'an unchanged state is never asked twice')
  views[0].turnSettled()
  const restaged = { ...request, refs: [REFS[0], { ...REFS[1], rev: 'c'.repeat(40) }] }
  assert.equal(await views[0].ask(KEY, card, restaged), true)
  assert.equal(views[0].askState(restaged), 'asked')
  assert.equal(posted.size, 2)
})

test('identical blocks share their asks; the fingerprint is saved content, not position', async () => {
  const fence = { app: 'contribute', intent: 'chat:contributions', title: 'Cards', interaction: 'inline', items: [{ title: 'A', intent: 'review:a-1' }] }
  const parse = value => appBlockFromToken({ type: 'code', lang: 'mobius-app', text: JSON.stringify(value) })
  assert.equal(blockFingerprint(parse(fence)), blockFingerprint(parse(fence)))
  assert.notEqual(blockFingerprint(parse(fence)), blockFingerprint(parse({ ...fence, items: [{ title: 'B', intent: 'review:b-1' }] })))
  assert.equal(blockFingerprint(parse(fence)), blockFingerprint(parse({ ...fence, items: [{ title: 'Renamed', intent: 'review:a-1' }] })),
    'a row title is not part of it')
})

test('a failed send frees the button', async () => {
  const chat = askChat(async () => { throw new Error('offline') })
  const card = {}
  chat.place(KEY, card, node(1))
  assert.equal(await chat.ask(KEY, card, request), false)
  assert.equal(chat.askState(request), 'ready')
  const refused = askChat(async () => false)
  refused.place(KEY, card, node(1))
  assert.equal(await refused.ask(KEY, card, request), false)
})

test('one view shows the tapped card as sending, then asked until a turn settles', async () => {
  let release
  const chat = askChat(() => new Promise(resolve => { release = resolve }))
  const card = {}
  chat.place(KEY, card, node(1))
  const pending = chat.ask(KEY, card, request)
  assert.equal(chat.askState(request), 'sending')
  while (!release) await new Promise(resolve => setTimeout(resolve, 0))
  release(true)
  assert.equal(await pending, true)
  assert.equal(chat.askState(request), 'asked')
  chat.turnSettled()
  assert.equal(chat.askState(request), 'ready')
})

test('a late acknowledgement after its turn settled leaves no receipt (review repro)', async () => {
  let release
  const chat = askChat(() => new Promise(resolve => { release = resolve }))
  const card = {}
  chat.place(KEY, card, node(1))
  const pending = chat.ask(KEY, card, request)
  while (!release) await new Promise(resolve => setTimeout(resolve, 0))
  chat.turnSettled() // the queued ask's turn ran and ended before its POST answered
  assert.equal(chat.askState(request), 'sending', 'still in flight')
  release(true)
  assert.equal(await pending, true)
  assert.equal(chat.askState(request), 'ready', 'the expired receipt is not reinstated')
})

test('a receipt belongs to its ask: a new revision or a newer card is not locked by it', async () => {
  const { posted, send } = server()
  const chat = askChat(send)
  const [older, newer] = [{}, {}]
  chat.place(KEY, older, node(1))
  assert.equal(await chat.ask(KEY, older, request), true)
  assert.equal(chat.askState(request), 'asked')
  const restaged = { ...request, refs: [REFS[0], { ...REFS[1], rev: 'c'.repeat(40) }] }
  assert.equal(chat.askState(restaged), 'ready', 'a new rev starts ready')
  assert.equal(await chat.ask(KEY, older, restaged), true)
  chat.place(KEY, newer, node(2))
  const newerCard = { ...request, fingerprint: `${FINGERPRINT}-newer` }
  assert.equal(chat.askState(newerCard), 'ready', 'a newer card in the group starts ready')
  assert.equal(await chat.ask(KEY, newer, newerCard), true)
  assert.equal(posted.size, 3)
  // While one ask is in flight, another ask is not blocked by it.
  let release
  const slow = askChat(async (message, options) => { await new Promise(resolve => { release = resolve }); return send(message, options) })
  slow.place(KEY, newer, node(1))
  const first = slow.ask(KEY, newer, { ...request, refs: [{ ...REFS[0], rev: 'd'.repeat(40) }] })
  assert.equal(slow.askState(request), 'ready')
  while (!release) await new Promise(resolve => setTimeout(resolve, 0))
  release()
  assert.equal(await first, true)
})

test('only the newest card of its key may ask', async () => {
  const { posted, send } = server()
  const chat = askChat(send)
  const [older, newer] = [{}, {}]
  chat.place(KEY, older, node(1))
  chat.place(KEY, newer, node(2))
  assert.equal(await chat.ask(KEY, older, request), false)
  assert.equal(await chat.ask(KEY, newer, request), true)
  assert.equal(posted.size, 1)
  assert.equal(await createAppBlockChat('chat-1').ask(KEY, {}, request), false, 'no sender, no ask')
})

test('the block hook exposes its own card ask', async () => {
  const { posted, send } = server()
  const chat = askChat(send)
  let seen = null
  const Probe = () => { seen = useAppBlockChat(KEY); return null }
  renderToStaticMarkup(createElement(AppBlockChatContext.Provider, { value: chat }, createElement(Probe)))
  assert.equal(seen.askState(request), 'ready')
  renderToStaticMarkup(createElement(Probe))
  assert.equal(await seen.ask(request), false, 'outside a chat nothing can be asked')
  assert.equal(posted.size, 0)
})

test('the chat sends a card ask as an owner message that leaves the composer\'s draft alone', async () => {
  const { readFileSync } = await import('node:fs')
  const src = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  assert.equal((src.match(/return 'duplicate'/g) || []).length, 2, 'both send paths report a duplicate as such')
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
