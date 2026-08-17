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

function installBrowserEnvironment({ observers = [], frames = null } = {}) {
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    ResizeObserver: globalThis.ResizeObserver,
    MutationObserver: globalThis.MutationObserver,
    requestAnimationFrame: globalThis.requestAnimationFrame,
    cancelAnimationFrame: globalThis.cancelAnimationFrame,
  }
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
    constructor(callback) {
      this.callback = callback
      this.disconnected = false
      this.observed = new Set()
      observers.push(this)
    }
    observe(target) { this.observed.add(target) }
    unobserve(target) { this.observed.delete(target) }
    disconnect() { this.disconnected = true }
  }
  globalThis.MutationObserver = class {
    observe() {}
    disconnect() {}
  }
  globalThis.requestAnimationFrame = frames
    ? callback => {
        frames.push(callback)
        return frames.length
      }
    : () => 1
  globalThis.cancelAnimationFrame = () => {}
  return () => Object.assign(globalThis, previous)
}

function mountTailController(chatId) {
  const listeners = new Map()
  const lastUser = fakeElement({
    dataset: { cid: 'user-1', key: 'user-1' },
    offsetTop: 0,
    offsetHeight: 40,
  })
  const assistant = fakeElement({
    dataset: { key: 'assistant-tail' },
    offsetTop: 100,
    offsetHeight: 300,
  })
  const list = fakeElement({ offsetHeight: 400 })
  const scroll = fakeElement({
    scrollTop: 400,
    scrollHeight: 900,
    clientHeight: 500,
    addEventListener(type, listener) { listeners.set(type, listener) },
    removeEventListener(type, listener) {
      if (listeners.get(type) === listener) listeners.delete(type)
    },
    contains(node) {
      for (let current = node; current; current = current.parentElement) {
        if (current === this) return true
      }
      return false
    },
    querySelector(selector) {
      if (selector === '.chat__list') return list
      if (selector.includes('data-key="assistant-tail"')) return assistant
      if (selector.includes('data-cid="user-1"')) return lastUser
      return null
    },
    querySelectorAll(selector) {
      if (selector === '.chat__msg[data-key]') return [assistant]
      if (selector === '.chat__msg--user[data-cid]') return [lastUser]
      return []
    },
  })
  scroll.parentElement = fakeElement()
  const messages = [
    { role: 'user', cid: 'user-1', content: 'Question' },
    { role: 'assistant', cid: 'assistant-tail', content: 'Answer' },
  ]
  const hook = renderHook(useScrollMode, {
    chatId,
    scrollRef: { current: scroll },
    spacerRef: { current: fakeElement() },
    lastUserMsgRef: { current: lastUser },
    chatRef: { current: fakeElement() },
    footRef: { current: fakeElement({ offsetHeight: 80 }) },
    messages,
    messagesRef: { current: messages },
    loadingOlderRef: { current: false },
    initialEntryPhase: 'ready',
    ownsReadingPosition: true,
  })
  return { hook, listeners, scroll }
}


test('transcript changes keep one scroll owner after the first row mounts', () => {
  const observers = []
  const restoreBrowser = installBrowserEnvironment({ observers })

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
    restoreBrowser()
  }
})

test('nested controls cannot relatch the transcript while they own the input', () => {
  const restoreBrowser = installBrowserEnvironment()
  try {
    const { hook, listeners, scroll } = mountTailController('nested-input-owner')
    assert.equal(scroll.dataset.scrollMode, 'ANCHOR_AT')

    const editor = {
      parentElement: scroll,
      closest(selector) { return selector.startsWith('textarea,') ? this : null },
    }
    listeners.get('keydown')({
      type: 'keydown', key: 'End', shiftKey: false, target: editor,
    })
    assert.equal(scroll.dataset.scrollMode, 'ANCHOR_AT',
      'End moves the inline caret, not the outer conversation')

    const nested = {
      parentElement: scroll,
      scrollTop: 10,
      scrollHeight: 180,
      clientHeight: 60,
      closest(selector) {
        return selector === '[data-chat-scroll-region], .chat__scroll' ? this : null
      },
    }
    listeners.get('wheel')({
      type: 'wheel', deltaY: 80, shiftKey: false, target: nested,
    })
    assert.equal(scroll.dataset.scrollMode, 'ANCHOR_AT',
      'a nested vertical surface keeps the wheel while it can move')

    nested.scrollTop = 120
    listeners.get('wheel')({
      type: 'wheel', deltaY: 80, shiftKey: false, target: nested,
    })
    assert.equal(scroll.dataset.scrollMode, 'FOLLOW_BOTTOM',
      'the same gesture may chain to the transcript at the nested edge')
    hook.unmount()
  } finally {
    restoreBrowser()
  }
})

test('a no-scroll tail relatch preserves the queued send-time pin decision', () => {
  const restoreBrowser = installBrowserEnvironment()
  try {
    const { hook, listeners, scroll } = mountTailController('queued-pin-owner')
    const intent = hook.result.current.captureSendIntent({ isFirstUserMsg: false })
    assert.equal(intent.willPin, true)

    listeners.get('wheel')({
      type: 'wheel', deltaY: 80, shiftKey: false,
      target: { parentElement: scroll, closest: () => null },
    })
    assert.equal(scroll.dataset.scrollMode, 'FOLLOW_BOTTOM')

    hook.result.current.commitSendIntent({ cid: 'queued-user-2', intent })
    assert.equal(scroll.dataset.scrollMode, 'PIN_USER_MSG',
      'a clamped gesture expresses follow but is not a newer scroll generation')
    hook.unmount()
  } finally {
    restoreBrowser()
  }
})

test('a focused inline editor keeps one current owner across keyboard and growth races', () => {
  const scrollListeners = new Map()
  const frames = []
  const observers = []
  const restoreBrowser = installBrowserEnvironment({ observers, frames })

  let scroll
  const row = fakeElement({
    dataset: { key: 'assistant-question' },
    offsetTop: 100,
    offsetHeight: 300,
    getBoundingClientRect() {
      return { top: this.offsetTop - scroll.scrollTop, bottom: 400 - scroll.scrollTop }
    },
  })
  const lastUser = fakeElement({
    dataset: { cid: 'user-1', key: 'user-1' },
    offsetTop: 0,
    offsetHeight: 40,
  })
  const list = fakeElement({ offsetHeight: 400 })
  scroll = fakeElement({
    scrollTop: 40,
    scrollHeight: 900,
    clientHeight: 500,
    getBoundingClientRect() { return { top: 0, bottom: 500, height: 500 } },
    addEventListener(type, listener) { scrollListeners.set(type, listener) },
    removeEventListener(type, listener) {
      if (scrollListeners.get(type) === listener) scrollListeners.delete(type)
    },
    querySelector(selector) {
      if (selector === '.chat__list') return list
      if (selector.includes('data-key="assistant-question"')) return row
      if (selector.includes('data-cid="user-1"')) return lastUser
      return null
    },
    querySelectorAll(selector) {
      if (selector === '.chat__msg[data-key]') return [row]
      if (selector === '.chat__msg--user[data-cid]') return [lastUser]
      return []
    },
    contains(node) { return node === editor },
  })
  scroll.parentElement = fakeElement()
  const scrollRef = { current: scroll }
  const messages = [
    { role: 'user', cid: 'user-1', content: 'Question' },
    { role: 'assistant', cid: 'assistant-question', content: '' },
  ]
  const hookArgs = {
    chatId: 'inline-growth-lifecycle',
    scrollRef,
    spacerRef: { current: fakeElement() },
    lastUserMsgRef: { current: lastUser },
    chatRef: { current: fakeElement() },
    footRef: { current: fakeElement({
      offsetHeight: 70,
      getBoundingClientRect() {
        return { top: scroll.clientHeight - 70, bottom: scroll.clientHeight }
      },
    }) },
    messages,
    messagesRef: { current: messages },
    loadingOlderRef: { current: false },
    initialEntryPhase: 'ready',
    hasTranscript: true,
    ownsReadingPosition: true,
  }
  let editorHeight = 38
  let editorHeightReads = 0
  const editorContentTop = 320
  const editor = {
    dataset: { chatInlineEditor: 'question-answer' },
    get offsetHeight() {
      editorHeightReads += 1
      return editorHeight
    },
    getBoundingClientRect() {
      const top = editorContentTop - scroll.scrollTop
      return { top, bottom: top + editorHeight, height: editorHeight }
    },
    closest: () => null,
  }

  const flushFrames = () => {
    while (frames.length) frames.shift()()
  }

  try {
    const hook = renderHook(useScrollMode, hookArgs)
    assert.equal(typeof scrollListeners.get('beforeinput'), 'function')
    assert.equal(typeof scrollListeners.get('input'), 'function')
    assert.equal(observers.length, 1,
      'an unfocused chat installs no editor-specific observer')

    globalThis.document.activeElement = editor
    scrollListeners.get('focusin')({ target: editor })
    assert.equal(observers.length, 1,
      'the focused editor joins the chat’s existing resize transaction')
    assert.equal(observers[0].observed.has(editor), true)
    assert.equal(editorHeightReads, 0,
      'focus does not synchronously measure the editor')
    scroll.clientHeight = 300
    scroll.scrollTop = 140 // Native keyboard caret reveal moves the transcript.
    observers[0].callback()
    assert.equal(scroll.scrollTop, 140,
      'keyboard resize adopts the focused field’s caret-visible position')

    scrollListeners.get('beforeinput')({ target: editor })
    scroll.scrollTop = 160 // The keyboard completes its caret reveal on first input.
    scrollListeners.get('input')({ target: editor })
    flushFrames()
    assert.equal(scroll.scrollTop, 160,
      'a first letter that does not grow the field keeps the visible caret')
    assert.equal(editorHeightReads, 0,
      'ordinary text input performs no synchronous field-size reads')

    const beforeGrowth = scroll.scrollTop
    scrollListeners.get('beforeinput')({ target: editor })
    editorHeight = 72
    scrollListeners.get('input')({ target: editor })
    observers[0].callback([{
      target: editor,
      borderBoxSize: [{ blockSize: editorHeight }],
    }])
    assert.equal(scroll.scrollTop > beforeGrowth, true,
      'field growth moves the answer upward instead of extending below the composer')
    assert.equal(editor.getBoundingClientRect().bottom <= 222, true,
      'the whole multiline writing surface remains in the usable viewport')
    flushFrames()

    scrollListeners.get('beforeinput')({ target: editor })
    editorHeight = 100
    scrollListeners.get('input')({ target: editor })
    scroll.clientHeight = 260
    scroll.scrollTop = 200 // A later keyboard frame selects a newer caret hold.
    observers[0].callback([{
      target: editor,
      borderBoxSize: [{ blockSize: editorHeight }],
    }])
    assert.equal(editor.getBoundingClientRect().bottom <= 182, true,
      'a combined keyboard and growth frame reveals above the resized composer edge')
    flushFrames()

    scrollListeners.get('beforeinput')({ target: editor })
    editorHeight = 120
    scrollListeners.get('input')({ target: editor })
    scrollListeners.get('pointerdown')({
      type: 'pointerdown', pointerType: 'mouse', button: 0, target: editor,
    })
    scroll.scrollTop = 90
    scrollListeners.get('scroll')()
    observers[0].callback([{
      target: editor,
      borderBoxSize: [{ blockSize: editorHeight }],
    }])
    flushFrames()
    assert.equal(scroll.scrollTop, 90,
      'a newer reader gesture owns the viewport instead of the edit correction')

    hook.unmount()
    assert.equal(scrollListeners.has('beforeinput'), false)
    assert.equal(scrollListeners.has('input'), false)
  } finally {
    restoreBrowser()
  }
})
