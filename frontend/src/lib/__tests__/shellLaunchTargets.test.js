// Launch delivery must preserve explicit destinations without changing resume.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { subscribeShellLaunchTargets } from '../shellLaunchTargets.js'

globalThis.location = { origin: 'https://mobius.test' }

function harness(pending = []) {
  let consumer = null
  const listeners = new Set()
  const opened = []
  const launchQueue = {
    setConsumer(next) {
      consumer = next
      for (const targetURL of pending.splice(0)) consumer({ targetURL })
    },
  }
  const serviceWorker = {
    addEventListener(type, callback) {
      assert.equal(type, 'message')
      listeners.add(callback)
    },
    removeEventListener(type, callback) {
      assert.equal(type, 'message')
      listeners.delete(callback)
    },
  }
  return {
    opened,
    subscribe: () => subscribeShellLaunchTargets(target => opened.push(target), { launchQueue, serviceWorker }),
    launch(targetURL) { consumer({ targetURL }) },
    message(data) { for (const listener of listeners) listener({ data }) },
    listeners,
  }
}

test('a warm installed-app launch opens the requested chat, not the restored screen', () => {
  const h = harness()
  h.subscribe()
  h.launch('https://mobius.test/shell/?chat=waiting-chat&focus=question')
  assert.deepEqual(h.opened, [{ view: 'chat', chatId: 'waiting-chat', focusQuestion: true }])
})

test('a launch queued before shell readiness is consumed when navigation mounts', () => {
  const h = harness(['https://mobius.test/shell/?chat=cold-chat'])
  h.subscribe()
  assert.deepEqual(h.opened, [{ view: 'chat', chatId: 'cold-chat', focusQuestion: false }])
})

test('ordinary app-icon launches leave the last screen untouched', () => {
  const h = harness()
  h.subscribe()
  for (const url of ['https://mobius.test/shell/', '/shell/', '/', undefined]) h.launch(url)
  assert.deepEqual(h.opened, [])
})

test('launches and push messages share validation, app intents and question focus', () => {
  const h = harness()
  h.subscribe()
  const app = '/shell/?app=pages&intent=artifact:report'
  h.launch(`https://mobius.test${app}`)
  h.message({ type: 'notification-click', target: app })
  h.message({ type: 'notification-click', target: '/shell/?chat=answer&focus=question' })
  assert.deepEqual(h.opened, [
    { view: 'canvas', app: 'pages', intent: 'artifact:report' },
    { view: 'canvas', app: 'pages', intent: 'artifact:report' },
    { view: 'chat', chatId: 'answer', focusQuestion: true },
  ])
  for (const url of [
    'https://evil.test/shell/?chat=wrong', '//evil.test/shell/?chat=wrong',
    'javascript:alert(1)', '/shell/?chat=../wrong', '/apps/other/',
  ]) {
    h.launch(url)
    h.message({ type: 'notification-click', target: url })
  }
  h.message({ type: 'unrelated', target: '/shell/?chat=wrong' })
  assert.equal(h.opened.length, 3)
})

test('cleanup retires callbacks and remount handles each subsequent launch once', () => {
  const h = harness(['https://mobius.test/shell/?chat=first'])
  const stop = h.subscribe()
  stop()
  assert.equal(h.listeners.size, 0)
  h.launch('/shell/?chat=unmounted')
  h.message({ type: 'notification-click', target: '/shell/?chat=unmounted' })
  h.subscribe()
  h.launch('/shell/?chat=second')
  assert.deepEqual(h.opened.map(target => target.chatId), ['first', 'second'])
  assert.equal(h.listeners.size, 1)
})

test('browsers without LaunchQueue retain warm push-message navigation', () => {
  const opened = []
  let listener
  const stop = subscribeShellLaunchTargets(target => opened.push(target), {
    launchQueue: undefined,
    serviceWorker: {
      addEventListener(type, callback) { listener = callback },
      removeEventListener() {},
    },
  })
  listener({ data: { type: 'notification-click', target: '/shell/?chat=message' } })
  assert.equal(opened[0].chatId, 'message')
  stop()
  assert.doesNotThrow(() => subscribeShellLaunchTargets(() => {}, {
    launchQueue: undefined, serviceWorker: undefined,
  })())
})
