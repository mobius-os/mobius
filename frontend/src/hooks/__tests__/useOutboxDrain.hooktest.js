// Exercise the shell's real outbox subscription across an unavailable boot.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { IDBFactory } from 'fake-indexeddb'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'

test('outbox readiness owns restart suspension and answer-triggered follow-up delivery', async t => {
  const originals = Object.fromEntries(
    ['window', 'document', 'navigator', 'localStorage', 'fetch', 'indexedDB']
      .map(key => [key, Object.getOwnPropertyDescriptor(globalThis, key)]),
  )
  t.after(() => {
    for (const [key, descriptor] of Object.entries(originals)) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor)
      else delete globalThis[key]
    }
  })
  const token = `test.${Buffer.from(JSON.stringify({ sub: 'readiness-fixture', epoch: 1 })).toString('base64url')}.test`
  const values = new Map([['token', token]])
  globalThis.localStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  globalThis.window = new EventTarget()
  globalThis.document = Object.assign(new EventTarget(), { visibilityState: 'visible' })
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { onLine: true } })
  globalThis.indexedDB = new IDBFactory()
  let serviceReady = false
  let bootId = 'boot-a'
  let restart
  const posts = []
  let receive = () => Response.json({ status: 'queued' })
  globalThis.fetch = async (path, options = {}) => {
    if (path === '/api/ready') {
      return Response.json({ ready: serviceReady, boot_id: bootId }, { status: serviceReady ? 200 : 503 })
    }
    assert.equal(options.method, 'POST')
    posts.push(JSON.parse(options.body).cid)
    if (posts.length === 1) restart('boot-a')
    return receive(JSON.parse(options.body))
  }
  const connectivity = await import('../../lib/connectivityStore.js')
  restart = connectivity.setRestartPending
  const { enqueueIntent, markIntentLocallyQueued, listIntents, outboxPrincipalKey, clearOutboxForTests, retireIntent } = await import('../../components/ChatView/chatOutbox.js')
  const { default: useOutboxDrain } = await import('../useOutboxDrain.js')
  const principalKey = outboxPrincipalKey(token)
  for (const cid of ['first', 'second']) {
    assert.equal(await enqueueIntent({ chatId: 'chat', cid, principalKey, body: { cid, text: cid } }), true)
  }
  const hook = renderHook(useOutboxDrain)
  t.after(() => hook.unmount())
  await connectivity.verifyConnectivity()
  assert.deepEqual(posts, [], 'reachable 503 is not delivery readiness')
  assert.equal((await listIntents(principalKey)).length, 2)
  serviceReady = true
  await connectivity.verifyConnectivity()
  for (let n = 0; n < 40 && posts.length === 0; n++) await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(posts, ['first'])
  for (let n = 0; n < 40 && (await listIntents(principalKey)).length !== 1; n++) await new Promise(resolve => setImmediate(resolve))
  assert.equal((await listIntents(principalKey)).length, 1)
  assert.equal(connectivity.getDeliveryReadySnapshot(), false)
  await connectivity.verifyConnectivity()
  assert.deepEqual(posts, ['first'], 'old ready boot cannot release the second intent')
  bootId = 'boot-b'
  await connectivity.verifyConnectivity()
  for (let n = 0; n < 40 && posts.length !== 2; n++) await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(posts, ['first', 'second'])
  assert.equal(await enqueueIntent({ chatId: 'chat', cid: 'deferred', principalKey,
    locallyQueued: true, body: { cid: 'deferred', text: 'deferred' } }), true)
  for (let n = 0; n < 40 && posts.length !== 3; n++) await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(posts, ['first', 'second', 'deferred'], 'explicit deferred input wakes an already-ready shell')
  assert.equal(await enqueueIntent({ chatId: 'chat', cid: 'failed-attempt', principalKey,
    body: { cid: 'failed-attempt', text: 'failed' } }), true)
  await markIntentLocallyQueued('failed-attempt', { chatId: 'chat', principalKey })
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(posts, ['first', 'second', 'deferred'], 'failure projection is not another delivery request')
  await clearOutboxForTests()
  let timestamp = Date.now()
  t.mock.method(Date, 'now', () => timestamp++)
  for (const answerDelivery of ['replayed', 'interactive']) {
    serviceReady = false
    await connectivity.verifyConnectivity()
    const start = posts.length
    const followup = `${answerDelivery}-followup`
    const answer = `${answerDelivery}-answer`
    let answered = false
    receive = body => {
      if (body.cid === answer) answered = true
      if (body.cid === followup && !answered) {
        return Response.json({ detail: { code: 'pending_question_open' } }, { status: 409 })
      }
      return Response.json({ status: 'queued' })
    }
    await enqueueIntent({ chatId: 'chat', cid: followup, principalKey,
      locallyQueued: true, body: { cid: followup, content: 'B' } })
    if (answerDelivery === 'replayed') {
      await enqueueIntent({ chatId: 'chat', cid: answer, type: 'answer', principalKey,
        locallyQueued: true, body: { cid: answer, question_id: 'question', answers: { Choice: 'Yes' } } })
    }
    serviceReady = true
    await connectivity.verifyConnectivity()
    if (answerDelivery === 'interactive') {
      for (let n = 0; n < 40 && posts.length === start; n++) await new Promise(resolve => setImmediate(resolve))
      for (let n = 0; n < 10; n++) await new Promise(resolve => setImmediate(resolve))
      assert.deepEqual(posts.slice(start), [followup], 'an unresolved card does not busy-retry')
      await enqueueIntent({ chatId: 'chat', cid: answer, type: 'answer', principalKey,
        body: { cid: answer, question_id: 'question', answers: { Choice: 'Yes' } } })
      answered = true // The interactive HTTP owner accepted this exact answer.
      await retireIntent(answer, { chatId: 'chat', outcome: 'delivered' })
    }
    for (let n = 0; n < 60 && (await listIntents(principalKey)).length; n++) await new Promise(resolve => setImmediate(resolve))
    assert.deepEqual(await listIntents(principalKey), [])
    assert.deepEqual(posts.slice(start), answerDelivery === 'replayed'
      ? [followup, answer, followup] : [followup, followup],
    'answer acknowledgement uses the existing coalesced drain to release B without a new wake')
  }

})
