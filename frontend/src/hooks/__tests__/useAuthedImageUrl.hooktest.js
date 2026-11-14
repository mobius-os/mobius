/* An avatar is read once per page: a second Settings visit shows the cached image on its first render, with no new download. */
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { setTimeout as tick } from 'node:timers/promises'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import useAuthedImageUrl, { forgetAuthedImage } from '../useAuthedImageUrl.js'

async function settle(view) {
  for (let i = 0; i < 50 && view.result.current.status === 'loading'; i += 1) {
    await tick(5)
    view.rerender()
  }
}

function serveImages(t, { ok = true, type = 'image/png' } = {}) {
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url) => {
    requests.push(String(url))
    return new Response(ok ? new Blob(['png'], { type }) : 'missing', { status: ok ? 200 : 404 })
  })
  return requests
}

test('a revisit reuses the image and its URL without downloading it again', async t => {
  const requests = serveImages(t)
  forgetAuthedImage('/test/avatar')
  const first = renderHook(() => useAuthedImageUrl('/test/avatar', 'octocat'))
  assert.equal(first.result.current.status, 'loading')
  assert.equal(first.result.current.url, null)
  await settle(first)
  assert.equal(first.result.current.status, 'ready')
  const url = first.result.current.url
  assert.match(url, /^blob:/)
  first.unmount()

  const second = renderHook(() => useAuthedImageUrl('/test/avatar', 'octocat'))
  t.after(() => second.unmount())
  assert.equal(second.result.current.status, 'ready')
  assert.equal(second.result.current.url, url)
  assert.equal(requests.length, 1)
})

test('a missing image settles to none so the caller shows its own artwork', async t => {
  serveImages(t, { ok: false })
  forgetAuthedImage('/test/missing')
  const view = renderHook(() => useAuthedImageUrl('/test/missing', 'nobody'))
  t.after(() => view.unmount())
  await settle(view)
  assert.deepEqual(view.result.current, { url: null, status: 'none' })
})

test('a failed read shows the caller artwork now and retries on the next visit', async t => {
  let online = false
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url) => {
    requests.push(String(url))
    if (!online) throw new TypeError('network down')
    return new Response(new Blob(['png'], { type: 'image/png' }), { status: 200 })
  })
  forgetAuthedImage('/test/flaky')
  const first = renderHook(() => useAuthedImageUrl('/test/flaky', 'octocat'))
  await settle(first)
  assert.deepEqual(first.result.current, { url: null, status: 'none' })
  first.unmount()
  const failedReads = requests.length

  online = true
  const second = renderHook(() => useAuthedImageUrl('/test/flaky', 'octocat'))
  t.after(() => second.unmount())
  second.rerender()
  await settle(second)
  assert.equal(second.result.current.status, 'ready')
  assert.ok(requests.length > failedReads)
})

test('a server error is not remembered as "no image": the next visit reads again', async t => {
  let status = 502
  const requests = []
  t.mock.method(globalThis, 'fetch', async (url) => {
    requests.push(String(url))
    return status === 200
      ? new Response(new Blob(['png'], { type: 'image/png' }), { status })
      : new Response('upstream unavailable', { status })
  })
  forgetAuthedImage('/test/upstream')
  const first = renderHook(() => useAuthedImageUrl('/test/upstream', 'octocat'))
  await settle(first)
  assert.deepEqual(first.result.current, { url: null, status: 'none' })
  first.unmount()

  status = 200
  const second = renderHook(() => useAuthedImageUrl('/test/upstream', 'octocat'))
  t.after(() => second.unmount())
  second.rerender()
  await settle(second)
  assert.equal(second.result.current.status, 'ready')
  assert.equal(requests.length, 2)
})

test('no account means no request and no placeholder wait', t => {
  const requests = serveImages(t)
  const view = renderHook(() => useAuthedImageUrl('/test/avatar', ''))
  t.after(() => view.unmount())
  assert.deepEqual(view.result.current, { url: null, status: 'none' })
  assert.equal(requests.length, 0)
})

test('a changed photo is read again where it is shown, past the browser cache', async t => {
  const reads = []
  t.mock.method(globalThis, 'fetch', async (url, init = {}) => {
    reads.push(init.cache)
    return new Response(new Blob([`png${reads.length}`], { type: 'image/png' }), { status: 200 })
  })
  const view = renderHook(() => useAuthedImageUrl('/test/changed', 'https://account.example/avatar'))
  t.after(() => view.unmount())
  await settle(view)
  const before = view.result.current.url
  assert.ok(before)

  forgetAuthedImage('/test/changed') // e.g. a new upload kept the same address
  view.rerender()
  await settle(view)
  assert.equal(view.result.current.status, 'ready')
  assert.notEqual(view.result.current.url, before)
  assert.deepEqual(reads, ['default', 'no-cache'])
})
