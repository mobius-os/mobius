import assert from 'node:assert/strict'
import test from 'node:test'

import {
  projectPublishedContribution,
  publishContribution,
  publishContributionStack,
} from '../chatContributionPublication.js'

function response({ ok, status = 200, body = null }) {
  return { ok, status, json: async () => body }
}

test('both chat surfaces preserve a draft publication projection', () => {
  const payload = { records: [{ id: 'one', status: 'prepared', needs_attention: true }] }
  const projected = projectPublishedContribution(payload, 'one', {
    record: { status: 'draft' }, number: 7, url: 'https://github.com/o/r/pull/7',
  })
  assert.deepEqual(projected.records[0], {
    id: 'one', status: 'draft', needs_attention: false,
    number: 7, url: 'https://github.com/o/r/pull/7',
  })
})

test('a lost stack response reconciles every linked action before retrying', async () => {
  const records = [
    { id: 'one', status: 'prepared', action_key: 'old-one' },
    { id: 'two', status: 'prepared', action_key: 'old-two' },
  ]
  const outcome = await publishContributionStack({
    appId: 80,
    item: { kind: 'stack', records },
    publish: async () => { throw new Error('connection reset') },
    refetch: async () => ({ data: { stack_units: [{ records: [
      { ...records[0], status: 'draft', action_key: 'new-one' },
      records[1],
    ] }] } }),
  })
  assert.equal(outcome.kind, 'reconciled')
})

test('a completed stack reconciles from the full lifecycle after leaving stack units', async () => {
  const records = [
    { id: 'one', status: 'draft', action_key: 'stack-one' },
    { id: 'two', status: 'prepared', action_key: 'stack-two' },
  ]
  const outcome = await publishContributionStack({
    appId: 80,
    item: { kind: 'stack', records },
    publish: async () => { throw new Error('connection reset') },
    refetch: async () => ({ data: {
      records: [
        { id: 'unrelated', status: 'open' },
        { ...records[0], action_key: 'lifecycle-one' },
        { ...records[1], status: 'open', action_key: 'lifecycle-two' },
      ],
      stack_units: [],
    } }),
  })

  assert.equal(outcome.kind, 'reconciled')
  assert.deepEqual(outcome.records.map(record => record.id), ['one', 'two'])
  assert.equal(outcome.records[1].status, 'open')
})

test('a lost response reconciles a record that already advanced', async () => {
  const record = { id: 'one', status: 'prepared', updated_at: 'old' }
  const outcome = await publishContribution({
    appId: 80,
    record,
    autopilot: true,
    publish: async () => { throw new Error('connection reset') },
    refetch: async () => ({ data: { records: [{ ...record, status: 'open' }] } }),
  })
  assert.equal(outcome.kind, 'reconciled')
  assert.equal(outcome.record.status, 'open')
})

test('a lost response waits through durable submitting until success is visible', async () => {
  const record = { id: 'one', status: 'prepared', updated_at: 'old' }
  const states = [
    { ...record, status: 'submitting', updated_at: 'claim' },
    { ...record, status: 'open', updated_at: 'published' },
  ]
  let reads = 0
  const outcome = await publishContribution({
    appId: 80,
    record,
    autopilot: true,
    publish: async () => { throw new Error('response lost after claim') },
    refetch: async () => ({ data: { records: [states[reads++]] } }),
    wait: async () => {},
  })

  assert.equal(reads, 2)
  assert.equal(outcome.kind, 'reconciled')
  assert.equal(outcome.record.status, 'open')
})

test('a lost response waits through submitting and reports durable rollback error', async () => {
  const record = { id: 'one', status: 'prepared', updated_at: 'old' }
  const states = [
    { ...record, status: 'submitting', updated_at: 'claim' },
    {
      ...record,
      status: 'prepared',
      updated_at: 'rolled-back',
      last_submit_error: 'GitHub rejected the reviewed branch.',
      last_submit_error_code: 'push_rejected',
    },
  ]
  let reads = 0
  const outcome = await publishContribution({
    appId: 80,
    record,
    autopilot: false,
    publish: async () => { throw new Error('response lost after claim') },
    refetch: async () => ({ data: { records: [states[reads++]] } }),
    wait: async () => {},
  })

  assert.equal(reads, 2)
  assert.equal(outcome.kind, 'failed')
  assert.equal(outcome.record.status, 'prepared')
  assert.equal(outcome.failure.message, 'GitHub rejected the reviewed branch.')
  assert.equal(outcome.failure.code, 'push_rejected')
})

test('a still-submitting record remains pending instead of becoming success', async () => {
  const record = { id: 'one', status: 'prepared', updated_at: 'old' }
  let reads = 0
  const outcome = await publishContribution({
    appId: 80,
    record,
    autopilot: false,
    publish: async () => { throw new Error('response lost after claim') },
    refetch: async () => {
      reads += 1
      return { data: { records: [{ ...record, status: 'submitting' }] } }
    },
    wait: async () => {},
  })

  assert.equal(reads, 4)
  assert.equal(outcome.kind, 'pending')
  assert.equal(outcome.record.status, 'submitting')
})

test('an unchanged failure keeps its structured recovery evidence', async () => {
  const record = { id: 'one', status: 'prepared', updated_at: 'same' }
  const outcome = await publishContribution({
    appId: 80,
    record,
    autopilot: false,
    publish: async () => response({
      ok: false,
      status: 409,
      body: { detail: { code: 'review_refresh_needed', message: 'Refresh it', detail: 'Head moved' } },
    }),
    refetch: async () => ({ data: { records: [record] } }),
  })
  assert.deepEqual(outcome, {
    kind: 'failed',
    record,
    failure: {
      status: 409,
      code: 'review_refresh_needed',
      message: 'Refresh it',
      detail: 'Head moved',
    },
  })
})
