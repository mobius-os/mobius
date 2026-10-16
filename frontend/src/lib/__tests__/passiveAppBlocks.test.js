/** Passive admission uses only the installed host projection, never block input. */
import test from 'node:test'
import assert from 'node:assert/strict'
import { passiveAppBlockAllowed } from '../passiveAppBlocks.js'

test('only an accepted opt-in bound to a compiled revision admits passive execution', () => {
  const app = { capability_contract: { runtime: { 'chat.blocks.passive': { version: 1 } } }, passive_block_module_digest: 'a'.repeat(64) }
  assert.equal(passiveAppBlockAllowed(app), true)
  for (const value of [null, {}, { appBlockSessions: true }, { ...app, passive_block_module_digest: null },
    { ...app, passive_block_module_digest: 'bad' }, { ...app, capability_contract: null },
    { ...app, capability_contract: { runtime: { 'chat.blocks.passive': { version: true } } } }]) {
    assert.equal(passiveAppBlockAllowed(value), false)
  }
})
