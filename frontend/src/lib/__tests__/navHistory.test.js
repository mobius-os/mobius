import test from 'node:test'
import assert from 'node:assert/strict'

import {
  isMobiusNavState,
  isVisibleAppOwner,
  navEntryIndex,
  navEntryId,
  navState,
  navTraversalDirection,
  pushNavEntry,
  replaceNavEntry,
  updateCurrentNavEntry,
} from '../navHistory.js'

// navState / isMobiusNavState are pure — test them without globals first.

test('navState carries kind, position, and restorable route', () => {
  const route = { view: 'chat', chatId: 'chat-1', appId: null }
  assert.deepEqual(navState('drawer', { index: 4, route }), {
    __mobiusNav: true,
    kind: 'drawer',
    index: 4,
    route,
  })
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

test('navTraversalDirection compares tagged shell positions', () => {
  const at = (index) => navState('nav', { index })
  assert.equal(navTraversalDirection(at(2), at(1)), 'back')
  assert.equal(navTraversalDirection(at(1), at(2)), 'forward')
  assert.equal(navTraversalDirection(at(2), at(2)), 'same')
  assert.equal(navTraversalDirection({}, at(2)), 'unknown')
  assert.equal(navTraversalDirection(at(2), { __mobiusNav: true }), 'unknown')
  assert.equal(navEntryIndex(at(3)), 3)
  assert.equal(navTraversalDirection(
    { __mobiusNav: true },
    { __mobiusNav: true },
    { currentEntryIndex: 8, destinationEntryIndex: 9 },
  ), 'forward')
})

test('only the visible canvas app owns app-level history', () => {
  assert.equal(isVisibleAppOwner('canvas', 7, '7'), true)
  assert.equal(isVisibleAppOwner('chat', 7, 7), false)
  assert.equal(isVisibleAppOwner('settings', 7, 7), false)
  assert.equal(isVisibleAppOwner('canvas', 8, 7), false)
  assert.equal(isVisibleAppOwner('canvas', null, 7), false)
})

// pushNavEntry / replaceNavEntry write to BOTH stores. Mock the two browser
// globals (history + navigation) and assert each helper hits each store.

function installBrowserMocks({ withNavigation = true } = {}) {
  const history = {
    calls: [],
    state: undefined,
    pushState(state, title, url) {
      this.calls.push({ method: 'pushState', state, title, url })
      this.state = state
    },
    replaceState(state, title, url) {
      this.calls.push({ method: 'replaceState', state, title, url })
      this.state = state
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
    const route = { view: 'settings', chatId: null, appId: null }
    pushNavEntry('drawer', route)

    // Classic History API got the tag.
    assert.equal(history.calls.length, 1)
    assert.equal(history.calls[0].method, 'pushState')
    assert.deepEqual(history.calls[0].state, {
      __mobiusNav: true, kind: 'drawer', index: 0, route,
      entryId: history.calls[0].state.entryId,
    })
    assert.equal(navEntryId(history.calls[0].state), history.calls[0].state.entryId)

    // Navigation API got the SAME tag — this is what makes
    // e.destination.getState() return the tag so the drawer/back guard
    // passes the shell's own entries on modern Chrome / installed PWAs.
    assert.equal(navigation.calls.length, 1)
    assert.deepEqual(navigation.calls[0].state, history.calls[0].state)
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
    assert.deepEqual(history.calls[0].state, {
      __mobiusNav: true, kind: 'base', index: 0, route: null,
      entryId: history.calls[0].state.entryId,
    })
    // The base-entry reset must keep passing the manifest-scope URL.
    assert.equal(history.calls[0].url, '/shell/')

    assert.equal(navigation.calls.length, 1)
    assert.deepEqual(navigation.calls[0].state, history.calls[0].state)
    assert.equal(isMobiusNavState(navigation.getCurrentState()), true)
  } finally {
    clearBrowserMocks()
  }
})

test('pushes increment from the current shell position and retain routes', () => {
  const { history } = installBrowserMocks({ withNavigation: false })
  try {
    replaceNavEntry('base', '/shell/', { view: 'chat', chatId: 'a', appId: null })
    const first = pushNavEntry('nav', { view: 'settings', chatId: 'a', appId: null })
    const second = pushNavEntry('app', { view: 'canvas', chatId: 'a', appId: 7 })
    assert.equal(first.index, 1)
    assert.equal(second.index, 2)
    assert.equal(second.route.appId, 7)
    assert.equal(history.state, second)
  } finally {
    clearBrowserMocks()
  }
})

test('updateCurrentNavEntry preserves position and can promote drawer to nav', () => {
  const { history, navigation } = installBrowserMocks()
  try {
    replaceNavEntry('base', '/shell/')
    pushNavEntry('drawer', { view: 'chat', chatId: 'a', appId: null })
    const route = { view: 'settings', chatId: 'a', appId: null }
    const updated = updateCurrentNavEntry(route, { kind: 'nav' })
    assert.deepEqual(updated, {
      __mobiusNav: true, kind: 'nav', index: 1, route,
      entryId: updated.entryId,
    })
    assert.equal(history.state, updated)
    assert.equal(navigation.getCurrentState(), updated)
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

test('a shell push after a phantom entry continues from the last tagged cursor', () => {
  const { history } = installBrowserMocks({ withNavigation: false })
  try {
    const tagged = replaceNavEntry('base', '/shell/')
    history.state = null // descendant frame pushed an untagged joint entry
    const next = pushNavEntry('nav', null, { currentState: tagged })
    assert.equal(next.index, 1)
    assert.notEqual(next.entryId, tagged.entryId)
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
