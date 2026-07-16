import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'
import { test } from 'node:test'
import assert from 'node:assert/strict'

import { PIN_OFFSET, PIN_BOTTOM_ROOM } from '../chatContract.js'

// chatContract.js deliberately MIRRORS PIN_OFFSET / PIN_BOTTOM_ROOM rather than
// importing them from useScrollMode.js (importing that file would pull React and
// a module-load sessionStorage read into the pure, browser-injectable contract
// module). The mirror carries a prose "SYNC OBLIGATION" that nothing enforced —
// so a change to the real constants could silently desync the contract checker,
// making it validate against stale geometry. This replaces the promise with a
// check: read the real constants from source and assert the mirror matches.

const dir = dirname(fileURLToPath(import.meta.url))
const useScrollModeSrc = readFileSync(
  join(dir, '..', 'useScrollMode.js'), 'utf8')

function readConst(name) {
  const m = useScrollModeSrc.match(
    new RegExp(`const\\s+${name}\\s*=\\s*(-?\\d+)`))
  assert.ok(m, `could not find const ${name} in useScrollMode.js`)
  return Number(m[1])
}

test('chatContract PIN_OFFSET mirrors useScrollMode', () => {
  assert.equal(PIN_OFFSET, readConst('PIN_OFFSET'),
    'chatContract.PIN_OFFSET drifted from useScrollMode.js — update the mirror')
})

test('chatContract PIN_BOTTOM_ROOM mirrors useScrollMode', () => {
  assert.equal(PIN_BOTTOM_ROOM, readConst('PIN_BOTTOM_ROOM'),
    'chatContract.PIN_BOTTOM_ROOM drifted from useScrollMode.js — update the mirror')
})
