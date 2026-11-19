import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import useHelperPreviewDocked from '../useHelperPreviewDocked.js'

test('docking follows its owning pane and releases observation when closed', () => {
  const prior = globalThis.ResizeObserver
  const observers = []
  globalThis.ResizeObserver = class {
    constructor(resize) { this.resize = resize; observers.push(this) }
    observe(host) { this.host = host }
    disconnect() { this.closed = true }
  }
  const host = { clientWidth: 1200 }
  let hook
  try {
    hook = renderHook(useHelperPreviewDocked, host)
    assert.equal(hook.result.current, true)
    assert.equal(observers[0].host, host)
    host.clientWidth = 700
    observers[0].resize()
    assert.equal(hook.result.current, false)
    host.clientWidth = 1000
    observers[0].resize()
    assert.equal(hook.result.current, true)
    const other = { clientWidth: 700 }
    hook.rerender(other)
    assert.equal(observers[0].closed, true)
    observers[1].resize()
    assert.equal(hook.result.current, false)
    hook.unmount()
    assert.equal(observers[1].closed, true)
  } finally {
    hook?.unmount()
    globalThis.ResizeObserver = prior
  }
})
