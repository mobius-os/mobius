import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  APP_NAV_LOCATION_MAX_BYTES,
  encodeNavLocation,
  validNavLocationText,
} from '../appNavLocation.js'
import {
  clearAppNavLocation,
  clearAppNavLocations,
  readAppNavLocation,
  writeValidatedAppNavLocation,
} from '../appNavLocationStore.js'

function memoryStore(initial = {}) {
  const items = new Map(Object.entries(initial))
  return {
    get length() { return items.size },
    key: index => [...items.keys()][index] ?? null,
    getItem: key => (items.has(key) ? items.get(key) : null),
    setItem: (key, value) => { items.set(key, String(value)) },
    removeItem: key => { items.delete(key) },
    items,
  }
}

test('locations stay per app and per installation', () => {
  const store = memoryStore()
  writeValidatedAppNavLocation(1, 'nonce-a', '{"view":"one"}', store)
  writeValidatedAppNavLocation(2, 'nonce-b', '{"view":"two"}', store)

  assert.equal(readAppNavLocation(1, 'nonce-a', store), '{"view":"one"}')
  assert.equal(readAppNavLocation(2, 'nonce-b', store), '{"view":"two"}')
  assert.equal(readAppNavLocation(3, 'nonce-a', store), null)
  assert.equal(readAppNavLocation(1, 'nonce-b', store), null, 'a reused id or wiped data starts fresh')
})

test('a cleared report removes the entry', () => {
  const store = memoryStore()
  writeValidatedAppNavLocation(1, 'generation-a', '{"view":"one"}', store)
  writeValidatedAppNavLocation(1, 'generation-a', null, store)
  assert.equal(store.items.size, 0)
})

test('the shared wire validator rejects invalid reports before persistence', () => {
  const invalid = ['alert(1)', '{broken', { view: 'object' },
    JSON.stringify({ q: 'x'.repeat(APP_NAV_LOCATION_MAX_BYTES) })]
  for (const value of invalid) {
    assert.equal(validNavLocationText(value), null)
  }
})

test('a tampered stored entry is never handed to a frame', () => {
  const store = memoryStore({
    'mobius:app-nav-location:1': JSON.stringify({ instance: null, location: '{oops' }),
    'mobius:app-nav-location:2': '{not json',
  })
  assert.equal(readAppNavLocation(1, null, store), null)
  assert.equal(readAppNavLocation(2, null, store), null)
})

test('the size bound counts UTF-8 bytes of the JSON text', () => {
  const fits = 'é'.repeat((APP_NAV_LOCATION_MAX_BYTES - 2) / 2)
  assert.equal(encodeNavLocation(fits), JSON.stringify(fits))
  assert.throws(() => encodeNavLocation(`${fits}é`), RangeError)
  assert.equal(validNavLocationText(JSON.stringify(`${fits}é`)), null)
  assert.equal(encodeNavLocation(undefined), null)
  assert.equal(validNavLocationText('null'), null)
})

test('logout clears every app location and nothing else', () => {
  const store = memoryStore({ unrelated: 'keep' })
  writeValidatedAppNavLocation(1, 'generation-a', '{"view":"one"}', store)
  writeValidatedAppNavLocation(2, 'generation-b', '{"view":"two"}', store)
  clearAppNavLocations(store)
  assert.deepEqual([...store.items.keys()], ['unrelated'])
})

test('app data wipe or uninstall clears only that app bookmark', () => {
  const store = memoryStore({ unrelated: 'keep' })
  writeValidatedAppNavLocation(1, 'generation-a', '{"view":"one"}', store)
  writeValidatedAppNavLocation(2, 'generation-b', '{"view":"two"}', store)
  clearAppNavLocation(1, store)
  assert.equal(store.getItem('mobius:app-nav-location:1'), null)
  assert.equal(readAppNavLocation(2, 'generation-b', store), '{"view":"two"}')
  assert.equal(store.getItem('unrelated'), 'keep')
})

test('a frame without a known storage generation cannot overwrite or clear the saved place', () => {
  const store = memoryStore()
  const location = '{"view":"saved"}'
  writeValidatedAppNavLocation(1, 'generation-a', location, store)
  for (const generation of [null, undefined, '']) {
    writeValidatedAppNavLocation(1, generation, '{"view":"loading"}', store)
    writeValidatedAppNavLocation(1, generation, null, store)
    writeValidatedAppNavLocation(2, generation, '{"view":"loading"}', store)
    assert.equal(readAppNavLocation(1, 'generation-a', store), location)
    assert.equal(readAppNavLocation(1, generation, store), null)
    assert.equal(store.items.size, 1)
  }
})

for (const value of [NaN, Infinity, -Infinity]) {
  test(`nonfinite location ${value} throws instead of clearing only the runtime`, () => {
    assert.throws(() => encodeNavLocation(value), {
      name: 'TypeError',
      message: 'window.mobius.nav.setLocation: the location must be JSON-serializable',
    })
  })
}

test('only explicit null and undefined clear a location', () => {
  assert.equal(encodeNavLocation(null), null)
  assert.equal(encodeNavLocation(undefined), null)
  assert.throws(() => encodeNavLocation({ toJSON: () => null }), TypeError)
})
