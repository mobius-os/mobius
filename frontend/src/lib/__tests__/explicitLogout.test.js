import test from 'node:test'
import assert from 'node:assert/strict'

import { clearExplicitOwnerSession } from '../explicitLogout.js'

test('explicit logout revokes copied handoffs before local owner state', async () => {
  const calls = []
  await clearExplicitOwnerSession({
    stopInstallHandoffPreparation: async () => { calls.push('stop') },
    revokeInstallHandoffs: async () => { calls.push('revoke') },
    dropCredential: () => { calls.push('credential') },
    clearOwnerCache: async () => { calls.push('cache') },
  })

  assert.deepEqual(calls, ['stop', 'revoke', 'credential', 'cache'])
})

test('failed handoff revocation never strands ordinary local logout', async () => {
  const calls = []
  await clearExplicitOwnerSession({
    stopInstallHandoffPreparation: async () => { calls.push('stop') },
    revokeInstallHandoffs: async () => {
      calls.push('revoke')
      throw new Error('offline')
    },
    dropCredential: () => { calls.push('credential') },
    clearOwnerCache: async () => { calls.push('cache') },
  })

  assert.deepEqual(calls, ['stop', 'revoke', 'credential', 'cache'])
})

test('failed local preparation stop still reaches server revocation', async () => {
  const calls = []
  await clearExplicitOwnerSession({
    stopInstallHandoffPreparation: async () => {
      calls.push('stop')
      throw new Error('cancel failed')
    },
    revokeInstallHandoffs: async () => { calls.push('revoke') },
    dropCredential: () => { calls.push('credential') },
    clearOwnerCache: async () => { calls.push('cache') },
  })

  assert.deepEqual(calls, ['stop', 'revoke', 'credential', 'cache'])
})
