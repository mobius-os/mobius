import test from 'node:test'
import assert from 'node:assert/strict'

import { makeClipboard } from '../../runtime/clipboard.js'

function replaceGlobal(name, value) {
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, name)
  Object.defineProperty(globalThis, name, { configurable: true, value })
  return () => {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor)
    else delete globalThis[name]
  }
}

test('clipboard uses the async API when synchronous copy is unavailable', async () => {
  const writes = []
  const restoreNavigator = replaceGlobal('navigator', {
    clipboard: { writeText: async (value) => writes.push(value) },
  })
  try {
    assert.equal(await makeClipboard().writeText(42), true)
    assert.deepEqual(writes, ['42'])
  } finally {
    restoreNavigator()
  }
})

test('clipboard copies synchronously before an async denial can consume activation', async () => {
  const calls = []
  let asyncWrites = 0
  const textarea = {
    style: {},
    setAttribute: (...args) => calls.push(['attribute', ...args]),
    focus: () => calls.push(['focus']),
    select: () => calls.push(['select']),
    remove: () => calls.push(['remove']),
  }
  const restoreNavigator = replaceGlobal('navigator', {
    clipboard: { writeText: async () => { asyncWrites += 1; throw new Error('denied') } },
  })
  const restoreDocument = replaceGlobal('document', {
    createElement: () => textarea,
    body: { appendChild: (node) => calls.push(['append', node]) },
    execCommand: (command) => {
      calls.push(['command', command])
      return true
    },
  })
  try {
    assert.equal(await makeClipboard().writeText('copy me'), true)
    assert.deepEqual(calls.slice(-4), [
      ['focus'],
      ['select'],
      ['command', 'copy'],
      ['remove'],
    ])
    assert.equal(asyncWrites, 0)
  } finally {
    restoreDocument()
    restoreNavigator()
  }
})

test('clipboard reports failure and still removes its synchronous control', async () => {
  let removed = false
  const restoreNavigator = replaceGlobal('navigator', {})
  const restoreDocument = replaceGlobal('document', {
    createElement: () => ({
      style: {},
      setAttribute() {},
      focus() {},
      select() {},
      remove() { removed = true },
    }),
    body: { appendChild() {} },
    execCommand: () => { throw new Error('blocked') },
  })
  try {
    assert.equal(await makeClipboard().writeText('copy me'), false)
    assert.equal(removed, true)
  } finally {
    restoreDocument()
    restoreNavigator()
  }
})

test('clipboard cleanup cannot overturn a successful synchronous copy', async () => {
  let removed = false
  const restoreNavigator = replaceGlobal('navigator', {
    clipboard: { writeText: async () => { throw new Error('must not retry') } },
  })
  const restoreDocument = replaceGlobal('document', {
    activeElement: { focus: () => { throw new Error('detached') } },
    createElement: () => ({
      style: {},
      setAttribute() {},
      focus() {},
      select() {},
      remove() { removed = true },
    }),
    body: { appendChild() {} },
    execCommand: () => true,
  })
  try {
    assert.equal(await makeClipboard().writeText('copy me'), true)
    assert.equal(removed, true)
  } finally {
    restoreDocument()
    restoreNavigator()
  }
})

test('clipboard asks the attributed host when the opaque frame cannot copy', async () => {
  const listeners = new Map()
  let posted = null
  const parent = {
    postMessage(message) {
      posted = message
      queueMicrotask(() => listeners.get('message')?.({
        source: parent,
        origin: 'https://mobius.test',
        data: {
          type: 'moebius:clipboard-write-result',
          requestId: message.requestId,
          ok: true,
        },
      }))
    },
  }
  const restoreWindow = replaceGlobal('window', {
    parent,
    location: { origin: 'https://mobius.test' },
    addEventListener: (type, callback) => listeners.set(type, callback),
    removeEventListener: (type, callback) => {
      if (listeners.get(type) === callback) listeners.delete(type)
    },
  })
  const restoreNavigator = replaceGlobal('navigator', {})
  const restoreDocument = replaceGlobal('document', {
    createElement: () => { throw new Error('opaque frame') },
  })
  try {
    assert.equal(await makeClipboard().writeText('copy me'), true)
    assert.equal(posted.type, 'moebius:clipboard-write')
    assert.equal(posted.text, 'copy me')
    assert.equal(listeners.size, 0)
  } finally {
    restoreDocument()
    restoreNavigator()
    restoreWindow()
  }
})

test('clipboard treats empty input as an ordinary unsuccessful copy', async () => {
  let called = false
  const restoreNavigator = replaceGlobal('navigator', {
    clipboard: { writeText: async () => { called = true } },
  })
  try {
    assert.equal(await makeClipboard().writeText(null), false)
    assert.equal(await makeClipboard().writeText(''), false)
    assert.equal(called, false)
  } finally {
    restoreNavigator()
  }
})
