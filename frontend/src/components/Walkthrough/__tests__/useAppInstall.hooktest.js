import { afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import { useAppInstall } from '../useAppInstall.js'

const realFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = realFetch })

const CATALOG = new Map([
  ['maps', { id: 'maps', manifest_url: 'https://example.test/maps/mobius.json' }],
  ['notes', { id: 'notes', manifest_url: 'https://example.test/notes/mobius.json' }],
])

// Answers /apps/install and records every request.
function stubServer({ status = 200, body = {}, hold } = {}) {
  const calls = []
  globalThis.fetch = async (url, init) => {
    calls.push({ path: String(url).replace(/^.*\/api/, ''), body: init?.body ? JSON.parse(init.body) : null })
    if (hold) await hold
    return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
  }
  return calls
}

const tick = () => new Promise(resolve => setTimeout(resolve, 0))

test('one_tap_installs_with_no_check_first', async () => {
  const calls = stubServer({ body: { warnings: ['cron: later'] } })
  const { result } = renderHook(useAppInstall, CATALOG)
  await result.current.install('maps')
  assert.deepEqual(calls, [{ path: '/apps/install', body: { manifest_url: CATALOG.get('maps').manifest_url } }])
  assert.deepEqual(result.current.statusOf('maps'), { state: 'installed', warnings: ['cron: later'] })
})

test('a_refused_install_leaves_the_app_retryable', async () => {
  stubServer({ status: 422, body: { detail: 'Manifest is invalid.' } })
  const { result } = renderHook(useAppInstall, CATALOG)
  await result.current.install('maps')
  assert.equal(result.current.statusOf('maps').state, 'error')
  assert.match(result.current.statusOf('maps').error, /invalid/i)
  const calls = stubServer()
  await result.current.install('maps')
  assert.equal(calls.length, 1)
  assert.equal(result.current.statusOf('maps').state, 'installed')
})

test('an_app_missing_from_the_catalog_does_nothing', async () => {
  const calls = stubServer()
  const { result } = renderHook(useAppInstall, CATALOG)
  await result.current.install('unknown')
  assert.deepEqual(calls, [])
  assert.equal(result.current.statusOf('unknown'), null)
})

test('apps_install_independently_and_a_repeat_tap_is_ignored', async () => {
  let release
  const calls = stubServer({ hold: new Promise(resolve => { release = resolve }) })
  const { result } = renderHook(useAppInstall, CATALOG)
  const first = result.current.install('maps')
  const repeat = result.current.install('maps')
  const second = result.current.install('notes')
  await tick()
  assert.equal(result.current.statusOf('maps').state, 'installing')
  assert.equal(result.current.statusOf('notes').state, 'installing')
  release()
  await Promise.all([first, repeat, second])
  assert.deepEqual(calls.map(call => call.body.manifest_url).sort(), [CATALOG.get('maps').manifest_url, CATALOG.get('notes').manifest_url].sort())
  assert.equal(result.current.statusOf('maps').state, 'installed')
  assert.equal(result.current.statusOf('notes').state, 'installed')
})
