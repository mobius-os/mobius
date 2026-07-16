import assert from 'node:assert/strict'
import test from 'node:test'

import { api } from '../../api/client.js'
import { serviceSurfaceFrameUrl } from '../serviceSurface.js'

test('service surface client returns the JSON descriptor, not the Fetch response URL', async (t) => {
  const shellRequestUrl = 'https://mobius.example/api/local-services/tandoor/surface'
  const gatewaySurfaceUrl = 'https://services.mobius.example/services/tandoor/_mobius/surface'
  const originalFetch = globalThis.fetch

  globalThis.fetch = async (url) => ({
    ok: true,
    status: 200,
    url: String(url),
    json: async () => ({ service: 'tandoor', url: gatewaySurfaceUrl }),
  })
  t.after(() => { globalThis.fetch = originalFetch })

  const surface = await api.services.surface('tandoor')

  assert.equal(surface.url, gatewaySurfaceUrl)
  assert.notEqual(surface.url, shellRequestUrl)
  assert.deepEqual(surface, { service: 'tandoor', url: gatewaySurfaceUrl })
})

test('service surface client reports the API detail for a rejected response', async (t) => {
  const originalFetch = globalThis.fetch
  globalThis.fetch = async () => ({
    ok: false,
    status: 503,
    url: 'https://mobius.example/api/local-services/tandoor/surface',
    json: async () => ({ detail: 'Public service gateway is not configured' }),
  })
  t.after(() => { globalThis.fetch = originalFetch })

  await assert.rejects(
    api.services.surface('tandoor'),
    /Public service gateway is not configured/,
  )
})

test('each service open has a distinct adapter navigation identity', () => {
  const surfaceUrl = 'https://services.mobius.example/services/tandoor/_mobius/surface'
  const first = new URL(serviceSurfaceFrameUrl(surfaceUrl, 'instance-one'))
  const second = new URL(serviceSurfaceFrameUrl(surfaceUrl, 'instance-two'))

  assert.equal(first.origin, 'https://services.mobius.example')
  assert.equal(first.pathname, '/services/tandoor/_mobius/surface')
  assert.equal(first.searchParams.get('mobius_instance'), 'instance-one')
  assert.equal(first.hash, '#instance-one')
  assert.notEqual(first.href, second.href)
})
