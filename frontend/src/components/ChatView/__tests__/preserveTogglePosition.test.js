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

test('closing a disclosure primes the bottom spacer before React removes its body', () => {
  const originalMutationObserver = globalThis.MutationObserver
  const originalRaf = globalThis.requestAnimationFrame
  const originalGetComputedStyle = globalThis.getComputedStyle
  globalThis.MutationObserver = undefined
  globalThis.requestAnimationFrame = () => 1
  globalThis.getComputedStyle = () => ({ marginTop: '4px', marginBottom: '2px' })

  try {
    const spacer = { offsetHeight: 100, style: {} }
    const scroller = {
      scrollTop: 50,
      querySelector: selector => selector === '.spacer-dynamic' ? spacer : null,
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
    assert.equal(spacer.style.height, '166px')
  }
  finally {
    globalThis.MutationObserver = originalMutationObserver
    globalThis.requestAnimationFrame = originalRaf
    globalThis.getComputedStyle = originalGetComputedStyle
  }
})

test('opening a disclosure leaves normal spacer sizing alone', () => {
  const originalMutationObserver = globalThis.MutationObserver
  const originalRaf = globalThis.requestAnimationFrame
  globalThis.MutationObserver = undefined
  globalThis.requestAnimationFrame = () => 1

  try {
    const spacer = { offsetHeight: 100, style: {} }
    const scroller = {
      scrollTop: 50,
      querySelector: () => spacer,
    }
    const anchor = {
      parentElement: {},
      nextElementSibling: null,
      closest: () => scroller,
      getAttribute: () => 'false',
      getBoundingClientRect: () => ({ top: 120 }),
    }

    preserveTogglePosition(anchor)
    assert.equal(spacer.style.height, undefined)
  }
  finally {
    globalThis.MutationObserver = originalMutationObserver
    globalThis.requestAnimationFrame = originalRaf
  }
})

test('a fast close-open-close cycle unwinds provisional space instead of accumulating it', () => {
  const originalMutationObserver = globalThis.MutationObserver
  const originalRaf = globalThis.requestAnimationFrame
  const originalGetComputedStyle = globalThis.getComputedStyle
  globalThis.MutationObserver = undefined
  globalThis.requestAnimationFrame = () => 1
  globalThis.getComputedStyle = () => ({ marginTop: '4px', marginBottom: '2px' })

  try {
    const spacer = {
      style: { height: '100px' },
      get offsetHeight() { return Number.parseFloat(this.style.height) },
    }
    const scroller = {
      scrollTop: 50,
      querySelector: () => spacer,
    }
    const body = { getBoundingClientRect: () => ({ height: 60 }) }
    let expanded = 'true'
    const anchor = {
      parentElement: {},
      nextElementSibling: body,
      closest: () => scroller,
      getAttribute: () => expanded,
      getBoundingClientRect: () => ({ top: 120 }),
    }

    preserveTogglePosition(anchor)
    assert.equal(spacer.style.height, '166px')

    // Re-open before ResizeObserver has replaced the provisional value.
    expanded = 'false'
    preserveTogglePosition(anchor)
    assert.equal(spacer.style.height, '100px')

    // A second fast close reserves one body height, not two.
    expanded = 'true'
    preserveTogglePosition(anchor)
    assert.equal(spacer.style.height, '166px')
  }
  finally {
    globalThis.MutationObserver = originalMutationObserver
    globalThis.requestAnimationFrame = originalRaf
    globalThis.getComputedStyle = originalGetComputedStyle
  }
})

test('provisional cleanup never overwrites a newer authoritative spacer value', () => {
  const originalMutationObserver = globalThis.MutationObserver
  const originalRaf = globalThis.requestAnimationFrame
  const originalGetComputedStyle = globalThis.getComputedStyle
  globalThis.MutationObserver = undefined
  globalThis.requestAnimationFrame = () => 1
  globalThis.getComputedStyle = () => ({ marginTop: '0px', marginBottom: '0px' })

  try {
    const spacer = {
      style: { height: '100px' },
      get offsetHeight() { return Number.parseFloat(this.style.height) },
    }
    const scroller = { scrollTop: 0, querySelector: () => spacer }
    const body = { getBoundingClientRect: () => ({ height: 60 }) }
    let expanded = 'true'
    const anchor = {
      parentElement: {},
      nextElementSibling: body,
      closest: () => scroller,
      getAttribute: () => expanded,
      getBoundingClientRect: () => ({ top: 80 }),
    }

    preserveTogglePosition(anchor)
    assert.equal(spacer.style.height, '160px')

    // Simulate ResizeObserver publishing new settled geometry.
    spacer.style.height = '112px'
    expanded = 'false'
    preserveTogglePosition(anchor)
    assert.equal(spacer.style.height, '112px')
  }
  finally {
    globalThis.MutationObserver = originalMutationObserver
    globalThis.requestAnimationFrame = originalRaf
    globalThis.getComputedStyle = originalGetComputedStyle
  }
})
