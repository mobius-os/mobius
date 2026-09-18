import test from 'node:test'
import assert from 'node:assert/strict'
import { limitRecoveryCredit } from '../limitRecoveryCredit.js'

test('offers a deliberate early continuation only for a reported paid allowance', () => {
  assert.deepEqual(limitRecoveryCredit('claude', {
    state: 'ready', extra_usage: { enabled: true, available: true },
  }), {
    label: 'Paid extra usage is available',
    actionLabel: 'Continue with extra usage',
  })
  assert.deepEqual(limitRecoveryCredit('codex', {
    state: 'ready', credit_balance: '18.50 credits',
  }), {
    label: 'Paid credits are available',
    actionLabel: 'Continue with paid credits',
  })
  assert.deepEqual(limitRecoveryCredit('mobius', {
    state: 'ready', windows: [{ kind: 'api_credits', remaining_percent: 12 }],
  }), {
    label: 'Usage credits are available',
    actionLabel: 'Continue with available credits',
  })
})

test('does not infer a paid recovery path from a limit alone', () => {
  assert.equal(limitRecoveryCredit('claude', {
    state: 'ready', extra_usage: { enabled: false, available: false },
  }), null)
  assert.equal(limitRecoveryCredit('mobius', {
    state: 'ready', windows: [{ kind: 'api_credits', remaining_percent: 0 }],
  }), null)
})
