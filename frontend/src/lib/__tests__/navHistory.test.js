import test from 'node:test'
import assert from 'node:assert/strict'

import {
  isMobiusNavState,
  navState,
  pushNavEntry,
  replaceNavEntry,
} from '../navHistory.js'

// navState / isMobiusNavState are pure — test them without globals first.

test('navState tags with __mobiusNav and the kind', () => {
  assert.deepEqual(navState('drawer'), { __mobiusNav: true, kind: 'drawer' })
})

test('isMobiusNavState recognizes a tagged state', () => {
  assert.equal(isMobiusNavState(navState('app')), true)
})

test('isMobiusNavState rejects untagged / phantom states', () => {
  // The phantom guard relies on these all being false: undefined is what
  // the Navigation API returns for a classic-API-only entry, and {} is a
  // genuine iframe-pushed entry.
  assert.equal(isMobiusNavState(undefined), false)
  assert.equal(isMobiusNavState(null), false)
  assert.equal(isMobiusNavState({}), false)
  assert.equal(isMobiusNavState({ __mobiusNav: false }), false)
  assert.equal(isMobiusNavState({ kind: 'drawer' }), false)
})

// pushNavEntry / replaceNavEntry write to BOTH stores. Mock the two browser
// globals (history + navigation) and assert each helper hits each store.

function installBrowserMocks({ withNavigation = true } = {}) {
  const history = {
    calls: [],
    pushState(state, title, url) {
      this.calls.push({ method: 'pushState', state, title, url })
    },
    replaceState(state, title, url) {
      this.calls.push({ method: 'replaceState', state, title, url })
    },
  }
  globalThis.history = history

  let navigation = null
  if (withNavigation) {
    navigation = {
      calls: [],
      // Mirrors the real Navigation API: getState() reads ONLY what was
      // written through updateCurrentEntry, NOT history.pushState.
      _state: undefined,
      updateCurrentEntry({ state }) {
        this.calls.push({ state })
        this._state = state
      },
      getCurrentState() {
        return this._state
      },
    }
    globalThis.navigation = navigation
  } else {
    // Some browsers (Safari, older Chrome) have no Navigation API at all.
    delete globalThis.navigation
  }
  return { history, navigation }
}

function clearBrowserMocks() {
  delete globalThis.history
  delete globalThis.navigation
}

test('pushNavEntry writes the tag to BOTH the History and Navigation stores', () => {
  const { history, navigation } = installBrowserMocks()
  try {
    pushNavEntry('drawer')

    // Classic History API got the tag.
    assert.equal(history.calls.length, 1)
    assert.equal(history.calls[0].method, 'pushState')
    assert.deepEqual(history.calls[0].state, { __mobiusNav: true, kind: 'drawer' })

    // Navigation API got the SAME tag — this is what makes
    // e.destination.getState() return the tag so the drawer/back guard
    // passes the shell's own entries on modern Chrome / installed PWAs.
    assert.equal(navigation.calls.length, 1)
    assert.deepEqual(navigation.calls[0].state, { __mobiusNav: true, kind: 'drawer' })
    assert.equal(isMobiusNavState(navigation.getCurrentState()), true)
  } finally {
    clearBrowserMocks()
  }
})

test('replaceNavEntry writes the tag to BOTH stores and forwards the URL', () => {
  const { history, navigation } = installBrowserMocks()
  try {
    replaceNavEntry('base', '/shell/')

    assert.equal(history.calls.length, 1)
    assert.equal(history.calls[0].method, 'replaceState')
    assert.deepEqual(history.calls[0].state, { __mobiusNav: true, kind: 'base' })
    // The base-entry reset must keep passing the manifest-scope URL.
    assert.equal(history.calls[0].url, '/shell/')

    assert.equal(navigation.calls.length, 1)
    assert.deepEqual(navigation.calls[0].state, { __mobiusNav: true, kind: 'base' })
    assert.equal(isMobiusNavState(navigation.getCurrentState()), true)
  } finally {
    clearBrowserMocks()
  }
})

test('replaceNavEntry defaults the URL to "" (preserve current URL)', () => {
  const { history } = installBrowserMocks()
  try {
    replaceNavEntry('nav')
    assert.equal(history.calls[0].url, '')
  } finally {
    clearBrowserMocks()
  }
})

test('helpers degrade gracefully when the Navigation API is absent', () => {
  // Safari / older Chrome: no navigation global. The classic write must
  // still happen and nothing should throw.
  const { history } = installBrowserMocks({ withNavigation: false })
  try {
    assert.equal(typeof navigation, 'undefined')
    pushNavEntry('app')
    replaceNavEntry('base', '/shell/')
    assert.equal(history.calls.length, 2)
    assert.equal(history.calls[0].method, 'pushState')
    assert.equal(history.calls[1].method, 'replaceState')
  } finally {
    clearBrowserMocks()
  }
})

test('a phantom entry (written to neither store) stays untagged in both', () => {
  // Simulate an iframe pushing a classic-API entry the shell did NOT tag:
  // getState() returns undefined, so the guard suppresses it.
  const { navigation } = installBrowserMocks()
  try {
    // No helper called → navigation store never updated.
    assert.equal(isMobiusNavState(navigation.getCurrentState()), false)
  } finally {
    clearBrowserMocks()
  }
})
