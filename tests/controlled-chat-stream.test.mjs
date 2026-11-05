import assert from 'node:assert/strict'
import test from 'node:test'
import { createContext, runInContext } from 'node:vm'
import { installControlledChatStream } from './_controlledChatStream.mjs'

// Run the real browser init script, with Web Streams at its external boundary.
async function streamFixture() {
  const ordinaryRequests = []
  const window = {
    fetch(input) {
      ordinaryRequests.push(input)
      return Promise.resolve(new Response('ordinary request'))
    },
  }
  const context = createContext({ window, TextEncoder, ReadableStream, Response, DOMException })
  const page = {
    async addInitScript(fn, argument) {
      context.argument = argument
      runInContext(`(${fn})(argument)`, context)
    },
    async evaluate(fn, argument) {
      context.argument = argument
      return runInContext(`(${fn})(argument)`, context)
    },
  }
  return { window, ordinaryRequests, stream: await installControlledChatStream(page, 'live') }
}

const streamUrl = 'https://mobius.test/api/chats/abc-123/stream'
const decode = value => new TextDecoder().decode(value)

test('controlled stream stays open after initial events until explicitly finished', async () => {
  const { window, ordinaryRequests, stream } = await streamFixture()
  await window.fetch(streamUrl)
  assert.equal(ordinaryRequests.length, 1, 'unarmed setup must use the ordinary fetch boundary')
  await stream.arm()
  const response = await window.fetch(streamUrl)
  const reader = response.body.getReader()
  assert.match(decode((await reader.read()).value), /catch_up_done/)
  assert.match(decode((await reader.read()).value), /"content":"live"/)
  let ended = false
  const waiting = reader.read().then(result => { ended = true; return result })
  await Promise.resolve()
  assert.equal(ended, false, 'returning a response must not introduce EOF')
  await stream.emit({ type: 'text', content: 'still live' })
  assert.match(decode((await waiting).value), /still live/)
  await stream.finish()
  assert.match(decode((await reader.read()).value), /"type":"done"/)
  assert.equal((await reader.read()).done, true)
  assert.deepEqual({ ...await stream.state() }, { requests: 1, open: false, terminals: 1, aborts: 0 })
})

test('controlled stream aborts its reader rather than delivering a successful terminal event', async () => {
  const { window, stream } = await streamFixture()
  await stream.arm()
  const abort = new AbortController()
  const response = await window.fetch(streamUrl, { signal: abort.signal })
  const reader = response.body.getReader()
  await reader.read()
  await reader.read()
  const pendingRead = reader.read()
  const rejected = assert.rejects(pendingRead, { name: 'AbortError' })
  abort.abort()
  await rejected
  assert.deepEqual({ ...await stream.state() }, { requests: 1, open: false, terminals: 0, aborts: 1 })
})

test('an armed fixture does not intercept unrelated requests', async () => {
  const { window, ordinaryRequests, stream } = await streamFixture()
  await stream.arm()
  const response = await window.fetch('https://mobius.test/api/ready')
  assert.equal(await response.text(), 'ordinary request')
  assert.equal(ordinaryRequests.length, 1)
  assert.equal((await stream.state()).requests, 0)
})
