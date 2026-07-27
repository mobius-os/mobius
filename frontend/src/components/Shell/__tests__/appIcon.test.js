import test from 'node:test'
import assert from 'node:assert/strict'
import { appIconUrl } from '../../appIcon.js'

test('installed app chrome uses the raw transparent icon route with a bounded size', () => {
  const app = {
    id: 42,
    slug: 'example-app',
    updated_at: '2026-07-27T03:00:00+00:00',
    has_custom_icon: true,
  }

  assert.equal(
    appIconUrl(app, 64),
    '/api/apps/42/icon?size=64&v=2026-07-27T03%3A00%3A00%2B00%3A00',
  )
  assert.equal(
    appIconUrl(app),
    '/api/apps/42/icon?size=128&v=2026-07-27T03%3A00%3A00%2B00%3A00',
  )
  assert.equal(appIconUrl({ id: 7, has_custom_icon: false }), null)
  assert.equal(appIconUrl(null), null)
})
