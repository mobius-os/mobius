/**
 * Unit tests for the early PWA install-prompt capture.
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'

async function freshModule() {
  return import(new URL(`../installPrompt.js?t=${Math.random()}`, import.meta.url))
}

function makeTarget({ standalone = false, webInstall = null } = {}) {
  const handlers = new Map()
  return {
    navigator: { standalone: false, ...(webInstall ? { install: webInstall } : {}) },
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

test('uses Web Install for the current document when available', async () => {
  const installPrompt = await freshModule()
  let calls = 0
  const target = makeTarget({
    webInstall: async () => { calls += 1 },
  })
  installPrompt.startInstallPromptCapture(target)

  assert.equal(installPrompt.getInstallPromptSnapshot(), 'ready')
  assert.deepEqual(await installPrompt.requestInstall(), { outcome: 'accepted' })
  assert.equal(calls, 1)
  assert.equal(installPrompt.getInstallPromptSnapshot(), 'installed')
  assert.equal(installPrompt.getInstallObservedSnapshot(), true)
})

test('a failed Web Install attempt preserves beforeinstallprompt for a second tap', async () => {
  const installPrompt = await freshModule()
  const target = makeTarget({
    webInstall: async () => {
      throw Object.assign(new Error('experimental implementation failed'), {
        name: 'DataError',
      })
    },
  })
  let legacyCalls = 0
  installPrompt.startInstallPromptCapture(target)
  target.dispatch('beforeinstallprompt', {
    preventDefault() {},
    async prompt() {
      legacyCalls += 1
      return { outcome: 'accepted' }
    },
  })

  assert.deepEqual(await installPrompt.requestInstall(), {
    outcome: 'fallback-ready',
  })
  assert.equal(installPrompt.getInstallPromptSnapshot(), 'ready')
  assert.deepEqual(await installPrompt.requestInstall(), { outcome: 'accepted' })
  assert.equal(legacyCalls, 1)
})

test('Web Install cancellation remains retryable', async () => {
  const installPrompt = await freshModule()
  let calls = 0
  const target = makeTarget({
    webInstall: async () => {
      calls += 1
      throw Object.assign(new Error('cancelled'), { name: 'AbortError' })
    },
  })
  installPrompt.startInstallPromptCapture(target)

  assert.deepEqual(await installPrompt.requestInstall(), { outcome: 'dismissed' })
  assert.equal(installPrompt.getInstallPromptSnapshot(), 'ready')
  assert.deepEqual(await installPrompt.requestInstall(), { outcome: 'dismissed' })
  assert.equal(calls, 2)
})

// iOS reports standalone display mode inside the in-app browser it opens from
// an installed PWA — a page that is plainly not the installed app. That guess
// is fine for suppressing an install offer and catastrophic for announcing
// success: it told someone mid-install that their app was already added.
test('a standalone-looking launch suppresses the offer but claims nothing', async () => {
  const installPrompt = await freshModule()
  installPrompt.startInstallPromptCapture(makeTarget({ standalone: true }))

  assert.equal(installPrompt.getInstallPromptSnapshot(), 'installed')
  assert.equal(installPrompt.getInstallObservedSnapshot(), false)
})

test('an app-specific prompt outranks the standalone window guess', async () => {
  const installPrompt = await freshModule()
  const target = makeTarget({ standalone: true })
  let prevented = false
  installPrompt.startInstallPromptCapture(target)

  // The shell itself stays suppressed until Chromium offers a prompt for the
  // mini-app document loaded into the same standalone window.
  assert.equal(installPrompt.getInstallPromptSnapshot(), 'installed')
  target.dispatch('beforeinstallprompt', {
    preventDefault() { prevented = true },
    async prompt() { return { outcome: 'accepted' } },
  })

  assert.equal(prevented, true)
  assert.equal(installPrompt.getInstallPromptSnapshot(), 'ready')
  assert.deepEqual(await installPrompt.requestInstall(), { outcome: 'accepted' })
})

test('only a witnessed appinstalled event may be announced', async () => {
  const installPrompt = await freshModule()
  const target = makeTarget()
  installPrompt.startInstallPromptCapture(target)

  assert.equal(installPrompt.getInstallObservedSnapshot(), false)
  target.dispatch('appinstalled')
  assert.equal(installPrompt.getInstallObservedSnapshot(), true)
  assert.equal(installPrompt.getInstallPromptSnapshot(), 'installed')
})

test('subscribers are notified when an install is witnessed', async () => {
  const installPrompt = await freshModule()
  const target = makeTarget()
  installPrompt.startInstallPromptCapture(target)
  let notified = 0
  installPrompt.subscribeInstallPrompt(() => { notified += 1 })

  target.dispatch('appinstalled')
  assert.equal(notified, 1)
  assert.equal(installPrompt.getInstallObservedSnapshot(), true)
})
