/* Behavioral coverage for the ChatView scroll controller's React lifecycle. */

import test from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import useScrollMode from '../../components/ChatView/useScrollMode.js'

function fakeElement(fields = {}) {
  const listeners = new Map()
  return {
    clientHeight: 500,
    scrollHeight: 900,
    scrollTop: 400,
    offsetHeight: 0,
    offsetTop: 0,
    dataset: {},
    parentElement: null,
    style: { setProperty() {} },
    querySelector() { return null },
    querySelectorAll() { return [] },
    getBoundingClientRect() { return { top: 0, bottom: 0, height: 0 } },
    addEventListener(type, handler) { listeners.set(type, handler) },
    removeEventListener(type, handler) {
      if (listeners.get(type) === handler) listeners.delete(type)
    },
    ...fields,
  }
}

test('content-only streaming keeps the active reader gesture owner mounted', () => {
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    ResizeObserver: globalThis.ResizeObserver,
    MutationObserver: globalThis.MutationObserver,
    requestAnimationFrame: globalThis.requestAnimationFrame,
    cancelAnimationFrame: globalThis.cancelAnimationFrame,
  }
  const observers = []
  globalThis.window = {
    addEventListener() {},
    removeEventListener() {},
    visualViewport: null,
  }
  globalThis.document = {
    activeElement: null,
    visibilityState: 'visible',
    addEventListener() {},
    removeEventListener() {},
  }
  globalThis.ResizeObserver = class {
    constructor() {
      this.disconnected = false
      observers.push(this)
    }
    observe() {}
    disconnect() { this.disconnected = true }
  }
  globalThis.MutationObserver = class {
    observe() {}
    disconnect() {}
  }
  globalThis.requestAnimationFrame = () => 1
  globalThis.cancelAnimationFrame = () => {}

  const lastUser = fakeElement({ offsetTop: 120, offsetHeight: 40 })
  const list = fakeElement({ offsetHeight: 720 })
  const scroll = fakeElement({
    querySelector(selector) {
      return selector === '.chat__list' ? list : null
    },
    querySelectorAll(selector) {
      return selector === '.chat__msg--user[data-cid]' ? [lastUser] : []
    },
  })
  scroll.parentElement = fakeElement()
  const spacer = fakeElement()
  const chat = fakeElement()
  const foot = fakeElement({ offsetHeight: 80 })
  const base = {
    chatId: 'stream-lifecycle',
    scrollRef: { current: scroll },
    spacerRef: { current: spacer },
    lastUserMsgRef: { current: lastUser },
    chatRef: { current: chat },
    footRef: { current: foot },
    messagesRef: { current: [] },
    pendingMessagesLength: 0,
    loadingOlderRef: { current: false },
    initialEntryPhase: 'history',
    ownsReadingPosition: true,
  }

  try {
    const messages = [{ role: 'user', cid: 'u1', content: 'Question' }]
    base.messagesRef.current = messages
    const hook = renderHook(useScrollMode, { ...base, messages })
    assert.equal(observers.length, 1)

    const streamed = [{ ...messages[0], content: 'Question, still streaming' }]
    base.messagesRef.current = streamed
    hook.rerender({ ...base, messages: streamed })
    assert.equal(observers.length, 1,
      'content changes stay on the existing ResizeObserver and gesture owner')
    assert.equal(observers[0].disconnected, false)

    const nextRow = [...streamed, { role: 'assistant', content: 'Answer' }]
    base.messagesRef.current = nextRow
    hook.rerender({ ...base, messages: nextRow })
    assert.equal(observers.length, 2,
      'adding a transcript row may reinstall the DOM lifecycle')
    assert.equal(observers[0].disconnected, true)
    hook.unmount()
  } finally {
    Object.assign(globalThis, previous)
  }
})
