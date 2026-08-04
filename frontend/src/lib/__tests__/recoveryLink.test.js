import test from 'node:test'
import assert from 'node:assert/strict'
import { renderToStaticMarkup } from 'react-dom/server'

import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import { api } from '../../api/client.js'
import RecoveryLink, {
  RECOVERY_CONTROL_URL,
} from '../../components/ErrorBoundary/RecoveryLink.jsx'

const realStatus = api.platform.status

function jsonResponse(body, { ok = true } = {}) {
  return { ok, json: async () => body }
}

function railwayStatus() {
  return jsonResponse({ activation: { level: 'live', deployment: 'railway' } })
}

function selfHostedStatus() {
  return jsonResponse({ activation: { level: 'live', deployment: 'self_hosted' } })
}

/**
 * Installs the credentials and the platform-status reply the link will see.
 * `token: null` models a surface with no owner credentials at all.
 */
function installPlatform({ status, token = 'owner-token' }) {
  const calls = []
  // api.platform.status is a fetch wrapper, so it always answers with a promise.
  api.platform.status = async (...args) => {
    calls.push(args)
    return status()
  }
  globalThis.localStorage = { getItem: key => (key === 'token' ? token : null) }
  return {
    calls,
    restore() {
      api.platform.status = realStatus
      delete globalThis.localStorage
    },
  }
}

/**
 * Renders the component the app actually ships: renderHook supplies the hook
 * runtime and flushes the detection effect, then the returned (real) React
 * tree is serialized. renderToStaticMarkup alone never runs effects, so it
 * can only ever see the undetected state.
 */
async function renderLink(props = {}) {
  const view = renderHook(() => RecoveryLink(props))
  // The effect resolves the fetch and then awaits response.json(); let both
  // microtask hops land before reading what the link settled on.
  await new Promise(resolve => setTimeout(resolve, 0))
  const html = renderToStaticMarkup(view.result.current)
  view.unmount()
  return html
}

test('an undetected deployment keeps both external recovery routes', async () => {
  assert.equal(RECOVERY_CONTROL_URL, 'https://www.mobius.you/')
  const platform = installPlatform({ status: railwayStatus, token: null })
  try {
    const html = await renderLink()
    assert.deepEqual(platform.calls, [], 'no credentials must mean no owner-only read')
    assert.match(html, /class="errbound__recovery"/)
    assert.match(html, /If the problem continues after trying again/)
    assert.match(html, new RegExp(`href="${RECOVERY_CONTROL_URL}"`))
    assert.match(html, /target="_top"/)
    assert.match(html, /mobiusctl recovery start/)
    assert.doesNotMatch(html, /href="\/recover/)
  } finally {
    platform.restore()
  }
})

test('a detected deployment shows only the route that applies to it', async () => {
  const railway = installPlatform({ status: railwayStatus })
  try {
    const html = await renderLink()
    assert.equal(railway.calls.length, 1)
    assert.match(html, /managed on Railway/)
    assert.match(html, />Open Recovery in mobius\.you<\/a>/)
    assert.doesNotMatch(html, /mobiusctl recovery start/)
  } finally {
    railway.restore()
  }

  const selfHosted = installPlatform({ status: selfHostedStatus })
  try {
    const html = await renderLink()
    assert.match(html, /This is a self-hosted Möbius instance/)
    assert.match(html, /<code[^>]*>mobiusctl recovery start<\/code>/)
    assert.doesNotMatch(html, /mobius\.you/)
  } finally {
    selfHosted.restore()
  }
})

test('self-host guidance addresses the owner rather than a separate operator', async () => {
  const selfHosted = installPlatform({ status: selfHostedStatus })
  try {
    const html = await renderLink()
    assert.match(html, /Run this on the server:/)
    assert.doesNotMatch(html, /ask the server operator/i)
  } finally {
    selfHosted.restore()
  }

  const unresolved = installPlatform({ status: railwayStatus, token: null })
  try {
    const html = await renderLink()
    assert.match(html, /Self-hosted — run this on the server:/)
    assert.doesNotMatch(html, /ask the server operator/i)
  } finally {
    unresolved.restore()
  }
})

test('an unreadable platform status degrades to both routes instead of guessing', async () => {
  const unreadable = [
    ['a rejected read', () => Promise.reject(new Error('offline'))],
    ['a non-ok response', () => jsonResponse({ activation: { deployment: 'railway' } }, { ok: false })],
    ['an unparseable body', () => ({ ok: true, json: async () => { throw new Error('bad json') } })],
    ['a deployment the backend never emits', () => jsonResponse({ activation: { deployment: 'fly' } })],
    ['a status with no activation', () => jsonResponse({ state: 'up_to_date' })],
  ]
  for (const [label, status] of unreadable) {
    const platform = installPlatform({ status })
    try {
      const html = await renderLink()
      assert.equal(platform.calls.length, 1, label)
      assert.match(html, /Managed hosting:/, label)
      assert.match(html, /Self-hosted — run this on the server:/, label)
      assert.doesNotMatch(html, /managed on Railway/, label)
    } finally {
      platform.restore()
    }
  }
})

test('the link keeps its host surface class and lead', async () => {
  const platform = installPlatform({ status: selfHostedStatus })
  try {
    const html = await renderLink({
      className: 'standalone-app__recovery',
      lead: 'If the app still won’t open,',
    })
    assert.match(html, /class="standalone-app__recovery"/)
    assert.match(html, /If the app still won’t open/)
    assert.match(html, /This is a self-hosted Möbius instance/)
  } finally {
    platform.restore()
  }
})
