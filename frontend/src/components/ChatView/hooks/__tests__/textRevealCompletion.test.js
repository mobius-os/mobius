// Live text keeps its reveal cursor through provider finals and terminal handoff.
import { test, afterEach } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import useStreamConnection from '../../useStreamConnection.js'

const keys = ['window', 'document', 'sessionStorage', 'fetch', 'requestAnimationFrame', 'cancelAnimationFrame']
const original = Object.fromEntries(keys.map(key => [key, globalThis[key]]))
let hook, wire, frames, now, ended, attached
const settle = () => new Promise(resolve => setImmediate(resolve))

afterEach(async () => {
  hook?.unmount()
  wire?.close()
  await attached
  for (const [key, value] of Object.entries(original)) {
    if (value === undefined) delete globalThis[key]
    else globalThis[key] = value
  }
})

async function setup({ reducedMotion = false, replay = [] } = {}) {
  frames = new Map()
  now = performance.now()
  ended = []
  let id = 0
  globalThis.requestAnimationFrame = callback => { frames.set(++id, callback); return id }
  globalThis.cancelAnimationFrame = id => frames.delete(id)
  globalThis.window = Object.assign(new EventTarget(), {
    innerHeight: 800, matchMedia: () => ({ matches: reducedMotion }),
  })
  globalThis.document = Object.assign(new EventTarget(), { visibilityState: 'visible' })
  const values = new Map()
  globalThis.sessionStorage = {
    getItem: key => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: key => values.delete(key),
  }
  const body = new ReadableStream({ start(controller) { wire = controller } })
  globalThis.fetch = async () => new Response(body)
  hook = renderHook(chat => useStreamConnection(chat, {
    onStreamEnd: info => ended.push({ info, text: visible() }),
  }), 'reveal-fixture')
  attached = hook.result.current.connectToStream(true)
  for (const event of replay) send(event)
  send({ type: 'catch_up_done' })
  await settle()
}

function send(event) {
  wire.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n`))
}
function visible() {
  return hook.result.current.streamItems.filter(item => item.type === 'text').map(item => item.content).join('')
}
async function frame(ms = 1000 / 60) {
  now += ms
  const callbacks = [...frames.values()]
  frames.clear()
  callbacks.forEach(callback => callback(now))
  await settle()
}
async function finish() {
  for (let i = 0; i < 600 && !ended.length; i++) await frame()
  assert.equal(ended.length, 1)
  await attached
}

for (const finalOnly of [false, true]) {
  test(`${finalOnly ? 'final-only' : 'delta then final'} text keeps revealing after done without a terminal jump`, async () => {
    await setup()
    const text = 'The answer continues steadily. '.repeat(30)
    if (!finalOnly) send({ type: 'text', content: text.slice(0, -12), text_item_id: 'one' })
    await settle()
    await frame()
    const before = visible().length
    send({ type: 'text_final', content: text, text_item_id: 'one' })
    send({ type: 'done' })
    await settle()
    assert.equal(visible().length, before, 'neither final nor done flushes pending characters')
    assert.equal(ended.length, 0)
    assert.equal(hook.result.current.isStreaming, true)
    await frame()
    assert.ok(visible().length > before && visible().length < text.length)
    await finish()
    assert.equal(ended[0].text, text)
    assert.equal(visible(), text, 'no repeated prefix or dropped final suffix')
    assert.equal(hook.result.current.isStreaming, false)
    assert.equal(frames.size, 0, 'an idle chat must not leave a perpetual animation frame')
  })
}

test('authoritative correction still replaces incompatible deltas immediately', async () => {
  await setup()
  send({ type: 'text', content: 'Mistaken prefix', text_item_id: 'one' })
  await settle()
  await frame()
  send({ type: 'text_final', content: 'Corrected answer', text_item_id: 'one' })
  await settle()
  assert.equal(visible(), 'Corrected answer')
  send({ type: 'done' })
  await finish()
})

for (const interrupt of ['stop', 'chat-switch', 'hidden']) {
  test(`${interrupt} settles a pending reveal without dropping text or promoting an obsolete turn`, async () => {
    await setup()
    const text = 'A long answer. '.repeat(40)
    send({ type: 'text', content: text, text_item_id: 'one' })
    send({ type: 'done' })
    await settle()
    assert.equal(ended.length, 0)
    if (interrupt === 'hidden') {
      document.visibilityState = 'hidden'
      document.dispatchEvent(new Event('visibilitychange'))
    } else if (interrupt === 'chat-switch') {
      hook.rerender('another-chat')
    } else {
      hook.result.current.disconnect({ clearStreaming: true })
    }
    await attached
    if (interrupt === 'hidden') assert.equal(ended[0].text, text)
    else assert.equal(ended.length, 0, 'the old async completion lost ownership')
    if (interrupt === 'stop') assert.equal(visible(), text)
    if (interrupt === 'chat-switch') assert.equal(visible(), '')
    assert.equal(frames.size, 0)
  })
}

test('reduced motion reveals live text immediately without animation', async () => {
  await setup({ reducedMotion: true })
  send({ type: 'text', content: 'Visible without animation', text_item_id: 'one' })
  send({ type: 'text_final', content: 'Visible without animation.', text_item_id: 'one' })
  await settle()
  assert.equal(visible(), 'Visible without animation.')
  assert.equal(frames.size, 0)
  send({ type: 'done' })
  await finish()
})

test('a final that targets an earlier provider item preserves interleaved question ownership', async () => {
  await setup()
  send({ type: 'text', content: 'Before the question. ', text_item_id: 'one' })
  send({ type: 'question', question_id: 'q', questions: [{ question: 'Continue?' }] })
  send({ type: 'text', content: 'After the question.', text_item_id: 'one' })
  send({ type: 'text_final', content: 'Before the question. After the question.', text_item_id: 'one' })
  send({ type: 'done' })
  await settle()
  await finish()
  assert.equal(visible(), 'Before the question. After the question.')
  assert.deepEqual(hook.result.current.streamItems.map(item => item.type), ['text', 'question'])
})

test('reopened backlog remains instant, including authoritative final reconciliation', async () => {
  const text = 'Already delivered before reconnect. '.repeat(100)
  await setup({ replay: [
    { type: 'text', content: text.slice(0, -1), text_item_id: 'one' },
    { type: 'text_final', content: text, text_item_id: 'one' },
  ] })
  assert.equal(visible(), text)
  assert.equal(frames.size, 0)
  send({ type: 'done' })
  await finish()
})

test('a final-only reply after tool activity reveals instead of appearing in full', async () => {
  await setup()
  send({ type: 'tool_start', tool: 'Read', tool_use_id: 't' })
  send({ type: 'tool_end', tool_use_id: 't' })
  const text = 'The final answer after the tool. '.repeat(30)
  send({ type: 'text_final', content: text, text_item_id: 'one' })
  send({ type: 'done' })
  await settle()
  assert.equal(visible(), '')
  await frame()
  assert.ok(visible().length > 0 && visible().length < text.length)
  await finish()
  assert.equal(ended[0].text, text)
})

test('sparse low-throughput arrivals accumulate fractional credit instead of stalling', async () => {
  await setup()
  for (let i = 1; i <= 40; i++) {
    send({ type: 'text', content: 'a', text_item_id: 'one' })
    await settle()
    for (let j = 0; j < 12; j++) await frame()
    assert.equal(visible(), 'a'.repeat(i), 'every sparse character eventually appears')
  }
  send({ type: 'done' })
  await finish()
})
