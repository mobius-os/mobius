/**
 * Unit tests for the early PWA install-prompt capture.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

async function freshModule() {
  return import(new URL(`../installPrompt.js?t=${Math.random()}`, import.meta.url))
}

function makeTarget({ standalone = false } = {}) {
  const handlers = new Map()
  return {
    navigator: { standalone: false },
    matchMedia: () => ({ matches: standalone }),
    addEventListener(type, handler) {
      handlers.set(type, handler)
    },
    dispatch(type, event = {}) {
      handlers.get(type)?.(event)
    },
  }
}

test('captures and consumes a one-shot native install prompt', async () => {
  const installPrompt = await freshModule()
  const target = makeTarget()
  let prevented = false
  let promptCalls = 0
  installPrompt.startInstallPromptCapture(target)

  target.dispatch('beforeinstallprompt', {
    preventDefault() { prevented = true },
    async prompt() {
      promptCalls += 1
      return { outcome: 'accepted' }
    },
  })

  assert.equal(prevented, true)
  assert.equal(installPrompt.getInstallPromptSnapshot(), 'ready')
  assert.deepEqual(await installPrompt.requestInstall(), { outcome: 'accepted' })
  assert.equal(promptCalls, 1)
  assert.equal(installPrompt.getInstallPromptSnapshot(), 'manual')
  assert.deepEqual(await installPrompt.requestInstall(), { outcome: 'unavailable' })
  assert.equal(promptCalls, 1)
})

test('falls back to userChoice for older Chromium prompt results', async () => {
  const installPrompt = await freshModule()
  const target = makeTarget()
  installPrompt.startInstallPromptCapture(target)
  target.dispatch('beforeinstallprompt', {
    preventDefault() {},
    async prompt() {},
    userChoice: Promise.resolve({ outcome: 'dismissed' }),
  })

  assert.deepEqual(await installPrompt.requestInstall(), { outcome: 'dismissed' })
})

test('appinstalled and standalone launch suppress the install invitation', async () => {
  const captured = await freshModule()
  const target = makeTarget()
  captured.startInstallPromptCapture(target)
  target.dispatch('appinstalled')
  assert.equal(captured.getInstallPromptSnapshot(), 'installed')

  const standalone = await freshModule()
  standalone.startInstallPromptCapture(makeTarget({ standalone: true }))
  assert.equal(standalone.getInstallPromptSnapshot(), 'installed')
})

test('subscribers are notified when prompt availability changes', async () => {
  const installPrompt = await freshModule()
  const target = makeTarget()
  let changes = 0
  installPrompt.startInstallPromptCapture(target)
  const unsubscribe = installPrompt.subscribeInstallPrompt(() => { changes += 1 })

  target.dispatch('beforeinstallprompt', {
    preventDefault() {},
    async prompt() { return { outcome: 'dismissed' } },
  })
  await installPrompt.requestInstall()
  unsubscribe()
  target.dispatch('appinstalled')

  assert.equal(changes, 2)
})
