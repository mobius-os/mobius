import test from 'node:test'
import assert from 'node:assert/strict'
import {
  appIconIsReady,
  appIconUrl,
  preloadAppIcons,
} from '../../appIcon.js'

test('installed app chrome sizes the canonical icon reference from AppOut', () => {
  const app = {
    id: 42,
    slug: 'example-app',
    icon_url: '/api/apps/42/icon?v=2026-07-27T03%3A00%3A00%2B00%3A00',
  }

  assert.equal(
    appIconUrl(app, 64),
    '/api/apps/42/icon?v=2026-07-27T03%3A00%3A00%2B00%3A00&size=64',
  )
  assert.equal(
    appIconUrl(app),
    '/api/apps/42/icon?v=2026-07-27T03%3A00%3A00%2B00%3A00&size=128',
  )
  assert.equal(
    appIconUrl(app, null),
    '/api/apps/42/icon?v=2026-07-27T03%3A00%3A00%2B00%3A00',
    'callers can request the accepted original without rebuilding its identity',
  )
  assert.equal(appIconUrl({ id: 7, icon_url: null }), null)
  assert.equal(appIconUrl({ id: 7 }), null)
  assert.equal(appIconUrl(null), null)
})

test('shell icon warming preserves app order and removes duplicate asset work', async () => {
  const apps = [
    { icon_url: '/api/apps/1/icon?v=a' },
    { icon_url: '/api/apps/2/icon?v=b' },
    { icon_url: '/api/apps/1/icon?v=a' },
    { icon_url: null },
  ]
  const requested = []
  class FakeImage {
    set src(value) {
      requested.push(value)
      queueMicrotask(() => this.onload())
    }
  }
  await preloadAppIcons(apps, { ImageCtor: FakeImage })
  assert.deepEqual(requested, [
    '/api/apps/1/icon?v=a&size=128',
    '/api/apps/2/icon?v=b&size=128',
  ])
})

test('preloaded artwork is decoded and ready for the launcher first render', async () => {
  let decodeCount = 0
  class FakeImage {
    set src(value) {
      this.value = value
      queueMicrotask(() => this.onload())
    }
    decode() {
      decodeCount += 1
      return Promise.resolve()
    }
  }

  const app = { icon_url: '/api/apps/9/icon?v=ready' }
  const url = `${app.icon_url}&size=128`
  const result = await preloadAppIcons([app], { ImageCtor: FakeImage })
  assert.deepEqual(result, [true])
  assert.equal(decodeCount, 1)
  assert.equal(appIconIsReady(url), true)
})
