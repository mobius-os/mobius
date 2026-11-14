/* Helper recovery uses delegation admission and never sends composer content. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { retryHelperRecovery } from '../helperRecovery.js'

function fixture(body, status = 200) {
  const requests = []
  const request = async (...args) => {
    requests.push(args)
    return new Response(JSON.stringify(body), { status })
  }
  return { request, requests }
}

test('helper retry targets the exact delegation park instead of sending a manual chat continuation', async () => {
  const h = fixture({ retry_started: true, status: 'resuming' })
  assert.deepEqual(await retryHelperRecovery(h.request, 'helper/a', 'park-a'), { status: 'resuming', running: true })
  assert.deepEqual(h.requests, [[ '/delegations/helper%2Fa/retry', {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"run_token":"park-a"}',
  } ]])
})

test('lost acknowledgement can replay the exact token without treating an active replacement as a failure', async () => {
  const h = fixture({ retry_started: false, status: 'running', physical_run_id: 'successor' })
  assert.equal((await retryHelperRecovery(h.request, 'helper', 'old-park')).running, true)
  assert.equal(JSON.parse(h.requests[0][1].body).run_token, 'old-park')
})

for (const status of ['paused', 'completed', 'failed']) {
  test(`an admitted retry already ${status} does not leave the composer busy`, async () => {
    const h = fixture({ retry_started: true, status })
    assert.deepEqual(await retryHelperRecovery(h.request, 'helper', 'park'), { status, running: false })
  })
}

test('a newer park is refreshed, never retargeted or reported as accepted', async () => {
  const h = fixture({ retry_started: false, status: 'paused', physical_run_id: 'new-park' })
  await assert.rejects(retryHelperRecovery(h.request, 'helper', 'old-park'), { code: 'recovery_changed' })
  assert.equal(h.requests.length, 1)
})

for (const status of ['paused', 'cancelled', 'interrupted', 'completed', 'needs_input']) {
  test(`a no-op ${status} response explains recovery rather than claiming a start`, async () => {
    const h = fixture({ retry_started: false, status, physical_run_id: 'park' })
    await assert.rejects(retryHelperRecovery(h.request, 'helper', 'park'), error => {
      assert.equal(error.code, 'helper_recovery_unavailable')
      assert.match(error.message, /parent chat/)
      return true
    })
    assert.equal(h.requests.length, 1)
  })
}

test('helper retry preserves the server refusal reason', async () => {
  const h = fixture({ detail: { code: 'parent_held', message: 'Resume the parent Goal first.' } }, 409)
  await assert.rejects(retryHelperRecovery(h.request, 'helper', 'park'), {
    message: 'Resume the parent Goal first.', status: 409, code: 'helper_recovery_unavailable',
  })
})

test('ambiguous network failure never falls back to an ordinary send', async () => {
  let count = 0
  await assert.rejects(retryHelperRecovery(async () => {
    count += 1
    throw new Error('offline')
  }, 'helper', 'park'), /offline/)
  assert.equal(count, 1)
})

for (const status of ['accepted', 'retrying', 'starting']) {
  test(`a no-op ${status} response needs a physical replacement before showing running`, async () => {
    for (const physical_run_id of [null, 'park']) {
      const h = fixture({ retry_started: false, status, physical_run_id })
      await assert.rejects(retryHelperRecovery(h.request, 'helper', 'park'), {
        code: 'helper_recovery_unavailable',
      })
    }
    const replacement = fixture({ retry_started: false, status, physical_run_id: 'replacement' })
    assert.equal((await retryHelperRecovery(replacement.request, 'helper', 'park')).running, true)
  })
}
