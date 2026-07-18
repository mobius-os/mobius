import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createScreenWakeLockController } from '../screenWakeLock.js'

class FakeDocument extends EventTarget {
  visibilityState = 'visible'

  setVisibility(next) {
    this.visibilityState = next
    this.dispatchEvent(new Event('visibilitychange'))
  }
}

class FakeSentinel extends EventTarget {
  releaseCount = 0

  async release() {
    this.releaseCount += 1
    this.dispatchEvent(new Event('release'))
  }
}

async function flushPromises() {
  await Promise.resolve()
  await Promise.resolve()
}

test('holds a screen wake lock only while voice activity is active', async () => {
  const documentTarget = new FakeDocument()
  const sentinels = []
  const requestedTypes = []
  const manager = {
    async request(type) {
      requestedTypes.push(type)
      const sentinel = new FakeSentinel()
      sentinels.push(sentinel)
      return sentinel
    },
  }
  const controller = createScreenWakeLockController({ manager, documentTarget })

  controller.start()
  await flushPromises()
  assert.deepEqual(requestedTypes, ['screen'])
  assert.equal(sentinels[0].releaseCount, 0)

  controller.stop()
  await flushPromises()
  assert.equal(sentinels[0].releaseCount, 1)
})

test('reacquires the wake lock when an active session returns to the foreground', async () => {
  const documentTarget = new FakeDocument()
  const sentinels = []
  const manager = {
    async request() {
      const sentinel = new FakeSentinel()
      sentinels.push(sentinel)
      return sentinel
    },
  }
  const controller = createScreenWakeLockController({ manager, documentTarget })

  controller.start()
  await flushPromises()
  documentTarget.setVisibility('hidden')
  await flushPromises()
  assert.equal(sentinels[0].releaseCount, 1)

  documentTarget.setVisibility('visible')
  await flushPromises()
  assert.equal(sentinels.length, 2)

  controller.stop()
  await flushPromises()
  assert.equal(sentinels[1].releaseCount, 1)
})

test('waits for a hidden active session to become visible before requesting', async () => {
  const documentTarget = new FakeDocument()
  documentTarget.visibilityState = 'hidden'
  let requestCount = 0
  const manager = {
    async request() {
      requestCount += 1
      return new FakeSentinel()
    },
  }
  const controller = createScreenWakeLockController({ manager, documentTarget })

  controller.start()
  await flushPromises()
  assert.equal(requestCount, 0)

  documentTarget.setVisibility('visible')
  await flushPromises()
  assert.equal(requestCount, 1)

  controller.stop()
})

test('repeated starts do not acquire duplicate locks', async () => {
  const documentTarget = new FakeDocument()
  let requestCount = 0
  const manager = {
    async request() {
      requestCount += 1
      return new FakeSentinel()
    },
  }
  const controller = createScreenWakeLockController({ manager, documentTarget })

  controller.start()
  controller.start()
  await flushPromises()

  assert.equal(requestCount, 1)
  controller.stop()
})

test('releases a request that resolves after voice activity stops', async () => {
  const documentTarget = new FakeDocument()
  const sentinel = new FakeSentinel()
  let resolveRequest
  const manager = {
    request() {
      return new Promise(resolve => { resolveRequest = resolve })
    },
  }
  const controller = createScreenWakeLockController({ manager, documentTarget })

  controller.start()
  controller.stop()
  resolveRequest(sentinel)
  await flushPromises()

  assert.equal(sentinel.releaseCount, 1)
})

test('degrades quietly when the Screen Wake Lock API is unavailable', () => {
  const documentTarget = new FakeDocument()
  const controller = createScreenWakeLockController({ manager: null, documentTarget })

  assert.doesNotThrow(() => {
    controller.start()
    controller.stop()
  })
})
