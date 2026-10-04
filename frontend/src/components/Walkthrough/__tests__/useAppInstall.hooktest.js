import { afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import { useAppInstall } from '../useAppInstall.js'

const realFetch = globalThis.fetch
afterEach(() => { globalThis.fetch = realFetch })

const CATALOG = new Map([['maps', { id: 'maps', manifest_url: 'https://example.test/maps/mobius.json' }]])

// Answers /apps/preview and /apps/install in turn and records what was asked of each.
function stubServer({ preview, install = { status: 200, body: {} } }) {
  const calls = []
  globalThis.fetch = async (url, init) => {
    const path = String(url).replace(/^.*\/api/, '')
    calls.push({ path, body: init?.body ? JSON.parse(init.body) : null })
    const reply = path === '/apps/preview' ? preview : install
    return new Response(JSON.stringify(reply.body), { status: reply.status ?? 200, headers: { 'Content-Type': 'application/json' } })
  }
  return calls
}

const CONTRACT = { agent: { skills: ['maps-app.md'] }, data: {} }
const PREVIEW = { body: { capability_contract: CONTRACT, capability_digest: 'd1' } }

test('tapping_install_opens_the_access_review_without_installing_anything', async () => {
  const calls = stubServer({ preview: PREVIEW })
  const { result } = renderHook(useAppInstall, CATALOG)
  await result.current.begin('maps')
  assert.deepEqual(calls.map(call => call.path), ['/apps/preview'])
  assert.equal(result.current.confirmation.id, 'maps')
  assert.deepEqual(result.current.confirmation.rows.map(row => row.label), ['Agent skills'])
  assert.equal(result.current.statusOf('maps'), null)
})

test('confirming_installs_the_reviewed_access_and_marks_the_app_installed', async () => {
  const calls = stubServer({ preview: PREVIEW })
  const { result } = renderHook(useAppInstall, CATALOG)
  await result.current.begin('maps')
  result.current.approve()
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(calls.at(-1), { path: '/apps/install', body: { manifest_url: CATALOG.get('maps').manifest_url, reviewed_capability_digest: 'd1' } })
  assert.equal(result.current.confirmation, null)
  assert.equal(result.current.statusOf('maps').state, 'installed')
})

test('cancelling_the_review_installs_nothing', async () => {
  const calls = stubServer({ preview: PREVIEW })
  const { result } = renderHook(useAppInstall, CATALOG)
  await result.current.begin('maps')
  result.current.dismiss()
  assert.equal(result.current.confirmation, null)
  assert.deepEqual(calls.map(call => call.path), ['/apps/preview'])
})

test('access_that_changed_since_the_review_reopens_it_with_a_notice', async () => {
  stubServer({
    preview: PREVIEW,
    install: { status: 409, body: { detail: { code: 'capability_changed', capability_contract: { data: { manage_apps: true } }, capability_digest: 'd2' } } },
  })
  const { result } = renderHook(useAppInstall, CATALOG)
  await result.current.begin('maps')
  result.current.approve()
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.equal(result.current.confirmation.digest, 'd2')
  assert.match(result.current.confirmation.notice, /changed/)
  assert.equal(result.current.statusOf('maps'), null)
})

test('an_access_check_that_fails_leaves_the_app_retryable', async () => {
  stubServer({ preview: { status: 502, body: { detail: 'The store is unreachable.' } } })
  const { result } = renderHook(useAppInstall, CATALOG)
  await result.current.begin('maps')
  assert.equal(result.current.confirmation, null)
  assert.equal(result.current.statusOf('maps').state, 'error')
  assert.match(result.current.statusOf('maps').error, /unreachable/)
})

test('an_app_missing_from_the_catalog_does_nothing', async () => {
  const calls = stubServer({ preview: PREVIEW })
  const { result } = renderHook(useAppInstall, CATALOG)
  await result.current.begin('unknown')
  assert.equal(calls.length, 0)
  assert.equal(result.current.confirmation, null)
})


test('an_access_check_resolving_after_unmount_does_not_reopen_the_review', async () => {
  let resolveFetch
  globalThis.fetch = () => new Promise(resolve => { resolveFetch = resolve })
  const { result, unmount } = renderHook(useAppInstall, CATALOG)
  const checking = result.current.begin('maps')
  unmount()
  resolveFetch(new Response(JSON.stringify(PREVIEW.body), { status: 200 }))
  await checking
  assert.equal(result.current.confirmation, null)
})
