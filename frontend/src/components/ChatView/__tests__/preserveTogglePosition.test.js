import assert from 'node:assert/strict'
import test from 'node:test'

import { preserveTogglePosition } from '../preserveTogglePosition.js'

test('toggle position is corrected from the DOM mutation before rAF', () => {
  const originalMutationObserver = globalThis.MutationObserver
  const originalRaf = globalThis.requestAnimationFrame
  let mutationCallback = null
  let rafCallback = null
  let observedNode = null
  let observedOptions = null
  let disconnected = false

  globalThis.MutationObserver = class {
    constructor(callback) { mutationCallback = callback }
    observe(node, options) {
      observedNode = node
      observedOptions = options
    }
    disconnect() { disconnected = true }
  }
  globalThis.requestAnimationFrame = (callback) => {
    rafCallback = callback
    return 1
  }

  try {
    const body = {}
    const scroller = { scrollTop: 40 }
    let top = 120
    const anchor = {
      parentElement: {},
      nextElementSibling: body,
      closest: () => scroller,
      getBoundingClientRect: () => ({ top }),
    }

    preserveTogglePosition(anchor)
    assert.equal(observedNode, body)
    assert.deepEqual(observedOptions, {
      attributes: true,
      attributeFilter: ['hidden'],
    }, 'the body visibility commit must be the exact correction boundary')

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

test('FOLLOW_BOTTOM leaves toggle movement entirely to the scroll controller', () => {
  const originalMutationObserver = globalThis.MutationObserver
  const originalRaf = globalThis.requestAnimationFrame
  let observed = false
  let scheduled = false
  globalThis.MutationObserver = class {
    constructor() {}
    observe() { observed = true }
    disconnect() {}
  }
  globalThis.requestAnimationFrame = () => {
    scheduled = true
    return 1
  }

  try {
    const scroller = {
      dataset: { scrollMode: 'FOLLOW_BOTTOM' },
      scrollTop: 30,
      querySelector: () => ({ style: {}, offsetHeight: 10 }),
    }
    const anchor = {
      closest: () => scroller,
      getBoundingClientRect: () => ({ top: 80 }),
    }
    preserveTogglePosition(anchor, {})
    assert.equal(observed, false)
    assert.equal(scheduled, false)
    assert.equal(scroller.scrollTop, 30)
  } finally {
    globalThis.MutationObserver = originalMutationObserver
    globalThis.requestAnimationFrame = originalRaf
  }
})

test('disclosure preservation never reads or writes the dynamic spacer', () => {
  const originalMutationObserver = globalThis.MutationObserver
  const originalRaf = globalThis.requestAnimationFrame
  globalThis.MutationObserver = undefined
  globalThis.requestAnimationFrame = () => 1

  try {
    let queried = false
    const scroller = {
      scrollTop: 50,
      querySelector: () => {
        queried = true
        throw new Error('disclosures do not own reservation geometry')
      },
    }
    const body = { getBoundingClientRect: () => ({ height: 60 }) }
    const anchor = {
      parentElement: {},
      nextElementSibling: body,
      closest: () => scroller,
      getAttribute: name => name === 'aria-expanded' ? 'true' : null,
      getBoundingClientRect: () => ({ top: 120 }),
    }

    preserveTogglePosition(anchor)
    assert.equal(queried, false)
  }
  finally {
    globalThis.MutationObserver = originalMutationObserver
    globalThis.requestAnimationFrame = originalRaf
  }
})
