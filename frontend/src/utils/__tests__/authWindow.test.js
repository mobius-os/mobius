import test from 'node:test'
import assert from 'node:assert/strict'

import { closeAuthWindow, navigateAuthWindow, reserveAuthWindow } from '../authWindow.js'

function withWindow(stub, fn) {
  const previous = globalThis.window
  globalThis.window = stub
  try {
    return fn()
  } finally {
    globalThis.window = previous
  }
}

function fakeTab() {
  return {
    closed: false,
    document: { title: '', body: { style: {}, innerHTML: '' } },
  }
}

test('reserveAuthWindow opens a blank window synchronously and brands the wait page', () => {
  const calls = []
  const tab = fakeTab()
  withWindow({ open: (...args) => { calls.push(args); return tab } }, () => {
    const reserved = reserveAuthWindow('Opening Codex sign-in...')
    assert.equal(reserved, tab)
    // Blank + _blank, so the tab exists inside the user gesture and can be
    // pointed at the real URL once the async fetch resolves.
    assert.deepEqual(calls, [['', '_blank']])
    assert.equal(tab.document.title, 'Opening Codex sign-in...')
    assert.match(tab.document.body.innerHTML, /Opening sign-in/)
    assert.match(tab.document.body.innerHTML, /Möbius/)
  })
})

test('reserveAuthWindow returns null when the popup is blocked', () => {
  // The whole point of the module: a blocked popup must be reported, not
  // returned as a broken handle the caller then tries to navigate.
  withWindow({ open: () => null }, () => {
    assert.equal(reserveAuthWindow(), null)
  })
})

test('reserveAuthWindow returns null when window.open throws', () => {
  withWindow({ open: () => { throw new Error('blocked') } }, () => {
    assert.equal(reserveAuthWindow(), null)
  })
})

test('reserveAuthWindow returns null when there is no window at all', () => {
  withWindow(undefined, () => {
    assert.equal(reserveAuthWindow(), null)
  })
})

test('reserveAuthWindow keeps the tab when the placeholder cannot be written', () => {
  // Some browsers refuse writes to the interstitial document; the reserved tab
  // is still useful, so we must return it rather than fall into the null path.
  const tab = {
    closed: false,
    get document() { throw new Error('cross-origin') },
  }
  withWindow({ open: () => tab }, () => {
    assert.equal(reserveAuthWindow(), tab)
  })
})

test('navigateAuthWindow severs the opener before handing the tab to the provider', () => {
  const tab = {
    closed: false,
    opener: {},
    location: { replaced: '', replace(value) { this.replaced = value } },
  }
  assert.equal(navigateAuthWindow(tab, 'https://example.com/auth'), true)
  assert.equal(tab.location.replaced, 'https://example.com/auth')
  // Cut before navigation so the cross-origin sign-in page cannot reach back
  // and navigate us (reverse tabnabbing).
  assert.equal(tab.opener, null)
})

test('navigateAuthWindow refuses a missing, closed, or url-less target', () => {
  const tab = {
    closed: false,
    location: { replaced: '', replace(value) { this.replaced = value } },
  }
  assert.equal(navigateAuthWindow(null, 'https://example.com/auth'), false)
  assert.equal(navigateAuthWindow({ ...tab, closed: true }, 'https://example.com/auth'), false)
  assert.equal(navigateAuthWindow(tab, ''), false)
  assert.equal(tab.location.replaced, '')
})

test('navigateAuthWindow reports failure when the navigation throws', () => {
  // Caller uses the false return to close the now-useless blank tab.
  const tab = {
    closed: false,
    location: { replace() { throw new Error('denied') } },
  }
  assert.equal(navigateAuthWindow(tab, 'https://example.com/auth'), false)
})

test('closeAuthWindow is best-effort and skips already-closed windows', () => {
  let closed = 0
  closeAuthWindow({ closed: false, close() { closed += 1 } })
  closeAuthWindow({ closed: true, close() { closed += 1 } })
  closeAuthWindow(null)
  assert.equal(closed, 1)
})

test('closeAuthWindow swallows a close() that throws', () => {
  assert.doesNotThrow(() => {
    closeAuthWindow({ closed: false, close() { throw new Error('denied') } })
  })
})
