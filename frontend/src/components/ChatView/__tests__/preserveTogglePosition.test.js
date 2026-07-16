import assert from 'node:assert/strict'
import test from 'node:test'

import { preserveTogglePosition } from '../preserveTogglePosition.js'

test('toggle position is corrected from the DOM mutation before rAF', () => {
  const originalMutationObserver = globalThis.MutationObserver
  const originalRaf = globalThis.requestAnimationFrame
  let mutationCallback = null
  let rafCallback = null
  let observedNode = null
  let disconnected = false

  globalThis.MutationObserver = class {
    constructor(callback) { mutationCallback = callback }
    observe(node) { observedNode = node }
    disconnect() { disconnected = true }
  }
  globalThis.requestAnimationFrame = (callback) => {
    rafCallback = callback
    return 1
  }

  try {
    const parentElement = {}
    const scroller = { scrollTop: 40 }
    let top = 120
    const anchor = {
      parentElement,
      closest: () => scroller,
      getBoundingClientRect: () => ({ top }),
    }

    preserveTogglePosition(anchor)
    assert.equal(observedNode, parentElement)

    top = 155
    mutationCallback()
    assert.equal(scroller.scrollTop, 75)
    assert.equal(disconnected, true)

    // The scheduled fallback must not apply the correction a second time.
    rafCallback()
    assert.equal(scroller.scrollTop, 75)
  }
  finally {
    globalThis.MutationObserver = originalMutationObserver
    globalThis.requestAnimationFrame = originalRaf
  }
})

test('rAF remains a fallback when MutationObserver is unavailable', () => {
  const originalMutationObserver = globalThis.MutationObserver
  const originalRaf = globalThis.requestAnimationFrame
  let rafCallback = null

  globalThis.MutationObserver = undefined
  globalThis.requestAnimationFrame = (callback) => {
    rafCallback = callback
    return 1
  }

  try {
    const scroller = { scrollTop: 20 }
    let top = 80
    const anchor = {
      parentElement: {},
      closest: () => scroller,
      getBoundingClientRect: () => ({ top }),
    }

    preserveTogglePosition(anchor)
    top = 68
    rafCallback()
    assert.equal(scroller.scrollTop, 8)
  }
  finally {
    globalThis.MutationObserver = originalMutationObserver
    globalThis.requestAnimationFrame = originalRaf
  }
})
