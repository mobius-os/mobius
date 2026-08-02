import assert from 'node:assert/strict'
import test from 'node:test'

import {
  APP_FRAME_EDGE_PROBE_PATH,
  EDGE_POLICY_UNVERIFIED_MESSAGE,
  applyWithFreshEdgePolicy,
  probeAppFrameEdgePolicy,
} from '../platformEdgePreflight.js'

test('the edge probe bypasses app-code caches and reports the effective CSP', async () => {
  const calls = []
  const result = await probeAppFrameEdgePolicy(async (url, options) => {
    calls.push({ url, options })
    return {
      status: 404,
      headers: new Headers({
        'Content-Security-Policy': "default-src 'self'; script-src 'self' blob:",
      }),
    }
  }, () => 42)

  assert.deepEqual(calls, [{
    url: `${APP_FRAME_EDGE_PROBE_PATH}?mobius_edge_preflight=42`,
    options: {
      method: 'HEAD',
      cache: 'no-store',
      credentials: 'same-origin',
    },
  }])
  assert.deepEqual(result, {
    path: APP_FRAME_EDGE_PROBE_PATH,
    content_security_policy: "default-src 'self'; script-src 'self' blob:",
  })
})

test('a policy-free edge is represented explicitly rather than as a failed probe', async () => {
  const result = await probeAppFrameEdgePolicy(async () => ({
    status: 404,
    headers: new Headers(),
  }), () => 42)

  assert.deepEqual(result, {
    path: APP_FRAME_EDGE_PROBE_PATH,
    content_security_policy: null,
  })
})

test('apply carries the policy measured on this attempt, not a cached one', async () => {
  const payloads = []
  const probed = {
    path: APP_FRAME_EDGE_PROBE_PATH,
    content_security_policy: "script-src 'self' blob:",
  }

  const outcome = await applyWithFreshEdgePolicy(
    { plan_id: 'p1', current_sha: 'aaa', target_sha: 'bbb' },
    {
      probe: async () => probed,
      apply: async payload => {
        payloads.push(payload)
        return { ok: true }
      },
    },
  )

  assert.deepEqual(payloads, [{
    plan_id: 'p1',
    current_sha: 'aaa',
    target_sha: 'bbb',
    edge_preflight: probed,
  }])
  assert.deepEqual(outcome, { response: { ok: true } })
})

test('an unreadable edge stops the update before anything is applied', async () => {
  let applied = 0

  const outcome = await applyWithFreshEdgePolicy(
    { plan_id: 'p1' },
    {
      probe: async () => { throw new Error('offline') },
      apply: async () => { applied += 1 },
    },
  )

  assert.equal(applied, 0)
  assert.deepEqual(outcome, { error: EDGE_POLICY_UNVERIFIED_MESSAGE })
})
