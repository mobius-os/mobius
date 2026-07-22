import test from 'node:test'
import assert from 'node:assert/strict'

import { settleBackgroundAgentSave } from '../backgroundAgentSave.js'

test('a stale request still reports its HTTP failure before freshness', async () => {
  let freshnessChecks = 0
  const response = {
    ok: false,
    async json() { return { detail: 'Companion settings were not saved.' } },
  }

  await assert.rejects(
    settleBackgroundAgentSave(response, () => {
      freshnessChecks += 1
      return true
    }),
    /Companion settings were not saved/,
  )
  assert.equal(freshnessChecks, 0)
})

test('a successful superseded request may settle as stale', async () => {
  let freshnessChecks = 0
  const result = await settleBackgroundAgentSave(
    { ok: true },
    () => {
      freshnessChecks += 1
      return true
    },
  )

  assert.deepEqual(result, { stale: true })
  assert.equal(freshnessChecks, 1)
})
