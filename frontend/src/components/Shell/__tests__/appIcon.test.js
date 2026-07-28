import test from 'node:test'
import assert from 'node:assert/strict'
import { appIconUrl } from '../../appIcon.js'

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
