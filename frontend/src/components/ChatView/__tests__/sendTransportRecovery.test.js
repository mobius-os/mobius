import test from 'node:test'
import assert from 'node:assert/strict'

import { sendWithAmbiguityRecovery } from '../sendTransportRecovery.js'

const ambiguous = error => error?.name === 'ChatTransportError'

test('a lost acknowledgement retries the exact send once and confirms reachability', async () => {
  const calls = []
  let reachableReports = 0
  const response = await sendWithAmbiguityRecovery({
    send: async () => {
      calls.push('send')
      if (calls.length === 1) {
        const error = new Error('lost response')
        error.name = 'ChatTransportError'
        throw error
      }
      return { ok: true, status: 200 }
    },
    verifyReachability: async () => {
      calls.push('verify')
      return true
    },
    reportReachable: () => { reachableReports += 1 },
    isAmbiguousError: ambiguous,
  })

  assert.equal(response.status, 200)
  assert.deepEqual(calls, ['send', 'verify', 'send'])
  assert.equal(reachableReports, 1)
})

test('a confirmed outage leaves the draft recovery path in control', async () => {
  let sends = 0
  const error = new Error('offline')
  error.name = 'ChatTransportError'

  await assert.rejects(
    sendWithAmbiguityRecovery({
      send: async () => {
        sends += 1
        throw error
      },
      verifyReachability: async () => false,
      reportReachable: () => assert.fail('offline must not report reachable'),
      isAmbiguousError: ambiguous,
    }),
    error,
  )
  assert.equal(sends, 1)
})

test('HTTP failures are definitive and are never replayed', async () => {
  let sends = 0
  const error = Object.assign(new Error('HTTP 503'), { name: 'ChatHttpError' })

  await assert.rejects(
    sendWithAmbiguityRecovery({
      send: async () => {
        sends += 1
        throw error
      },
      verifyReachability: async () => assert.fail('HTTP response proves reachability'),
      isAmbiguousError: ambiguous,
    }),
    error,
  )
  assert.equal(sends, 1)
})

test('a second ambiguous failure stops after one safe replay', async () => {
  let sends = 0
  const error = new Error('still ambiguous')
  error.name = 'ChatTransportError'

  await assert.rejects(
    sendWithAmbiguityRecovery({
      send: async () => {
        sends += 1
        throw error
      },
      verifyReachability: async () => true,
      isAmbiguousError: ambiguous,
    }),
    error,
  )
  assert.equal(sends, 2)
})
