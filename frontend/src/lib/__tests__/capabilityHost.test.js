import { test } from 'node:test'
import assert from 'node:assert/strict'

import { createCapabilityHost } from '../capabilityHost.js'

const CAPABILITY = 'test.echo'

function setup({ active = true, declared = true } = {}) {
  const sent = []
  const source = { id: 'frame' }
  let channel
  let controls = []
  const host = createCapabilityHost({
    providers: {
      [CAPABILITY]: {
        version: 1,
        async open(context) {
          channel = context.channel
          return { control: (action, value) => controls.push([action, value]) }
        },
      },
    },
    getDeclaration(name) {
      return declared && name === CAPABILITY
        ? { version: 1, lifecycle: 'active_frame' }
        : null
    },
    isActive: () => active,
    send(target, message, transfer) { sent.push({ target, message, transfer }) },
  })
  return {
    host,
    source,
    sent,
    setDeclared(value) { declared = value },
    get channel() { return channel },
    get controls() { return controls },
  }
}

function openMessage(overrides = {}) {
  return {
    type: 'moebius:capability-open',
    requestId: 'request-1',
    capability: CAPABILITY,
    version: 1,
    input: {},
    ...overrides,
  }
}

test('host binds a declared capability session to its exact source', async () => {
  const harness = setup()
  assert.equal(harness.host.handle(harness.source, openMessage()), true)
  await new Promise((resolve) => setImmediate(resolve))

  harness.channel.ready({ ready: true })
  harness.channel.event('progress', 0.5)
  harness.host.handle(harness.source, {
    type: 'moebius:capability-control', requestId: 'request-1',
    capability: CAPABILITY, action: 'finish',
  })
  harness.channel.result({ ok: true })

  assert.deepEqual(
    harness.sent.map((entry) => entry.message.type),
    [
      'moebius:capability-ready',
      'moebius:capability-event',
      'moebius:capability-result',
    ],
  )
  assert.deepEqual(harness.controls, [['finish', undefined]])
  assert.equal(harness.host.activeCount(), 0)
})

test('host refuses undeclared, inactive, and mismatched-version requests', () => {
  const undeclared = setup({ declared: false })
  undeclared.host.handle(undeclared.source, openMessage())
  assert.equal(undeclared.sent.at(-1).message.code, 'undeclared')

  const inactive = setup({ active: false })
  inactive.host.handle(inactive.source, openMessage())
  assert.equal(inactive.sent.at(-1).message.code, 'not_active')

  const mismatch = setup()
  mismatch.host.handle(mismatch.source, openMessage({ version: 2 }))
  assert.equal(mismatch.sent.at(-1).message.code, 'version_mismatch')
})

test('host never truncates an invalid correlation id', () => {
  const harness = setup()
  const requestId = 'r'.repeat(121)

  assert.equal(harness.host.handle(
    harness.source,
    openMessage({ requestId }),
  ), true)
  assert.equal(harness.host.activeCount(), 0)
  assert.deepEqual(harness.sent, [])
})

test('control messages cannot cross session source boundaries', async () => {
  const harness = setup()
  harness.host.handle(harness.source, openMessage())
  await new Promise((resolve) => setImmediate(resolve))
  harness.host.handle({ id: 'hostile-frame' }, {
    type: 'moebius:capability-control', requestId: 'request-1',
    capability: CAPABILITY, action: 'cancel',
  })
  assert.deepEqual(harness.controls, [])
})

test('active-frame sessions are cancelled when their app becomes inactive', async () => {
  const harness = setup()
  harness.host.handle(harness.source, openMessage())
  await new Promise((resolve) => setImmediate(resolve))
  harness.host.deactivate()
  assert.deepEqual(harness.controls, [['cancel', undefined]])
})

test('browser permission errors become stable capability error codes', async () => {
  const sent = []
  const source = {}
  const host = createCapabilityHost({
    providers: {
      [CAPABILITY]: {
        version: 1,
        async open() {
          const error = new Error('Permission denied')
          error.name = 'NotAllowedError'
          throw error
        },
      },
    },
    getDeclaration: () => ({ version: 1 }),
    isActive: () => true,
    send(_target, message) { sent.push(message) },
  })
  host.handle(source, openMessage())
  await new Promise((resolve) => setImmediate(resolve))
  assert.equal(sent.at(-1).code, 'denied')
  assert.equal(sent.at(-1).name, 'NotAllowedError')
})

test('detaching a frame cancels only sessions opened by that exact source', async () => {
  const harness = setup()
  const otherSource = { id: 'other-frame' }
  harness.host.handle(harness.source, openMessage({ requestId: 'first' }))
  harness.host.handle(otherSource, openMessage({ requestId: 'second' }))
  await new Promise((resolve) => setImmediate(resolve))

  harness.host.detachSource(harness.source)

  assert.equal(harness.sent.some(({ target, message }) => (
    target === harness.source && message.type === 'moebius:capability-error'
      && message.code === 'aborted'
  )), true)
  assert.equal(harness.host.activeCount(), 1)
})

test('contract revocation cancels an already-open session', async () => {
  const harness = setup()
  harness.host.handle(harness.source, openMessage())
  await new Promise((resolve) => setImmediate(resolve))
  harness.setDeclared(false)

  harness.host.reconcile()

  assert.equal(harness.host.activeCount(), 0)
  assert.equal(harness.sent.at(-1).message.code, 'aborted')
})
