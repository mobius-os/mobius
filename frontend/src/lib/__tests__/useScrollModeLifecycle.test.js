/* Behavioral coverage for the ChatView scroll controller's React lifecycle. */

import test from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import { PIN_OFFSET } from '../../components/ChatView/chatContract.js'
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

function installBrowserEnvironment({
  observers = [],
  frames = null,
  windowListeners = null,
  documentListeners = null,
} = {}) {
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    ResizeObserver: globalThis.ResizeObserver,
    MutationObserver: globalThis.MutationObserver,
    localStorage: globalThis.localStorage,
    requestAnimationFrame: globalThis.requestAnimationFrame,
    cancelAnimationFrame: globalThis.cancelAnimationFrame,
  }
  const stored = new Map()
  globalThis.localStorage = {
    getItem(key) { return stored.has(key) ? stored.get(key) : null },
    setItem(key, value) { stored.set(key, String(value)) },
    removeItem(key) { stored.delete(key) },
    clear() { stored.clear() },
  }
  globalThis.window = {
    addEventListener(type, handler) { windowListeners?.set(type, handler) },
    removeEventListener(type, handler) {
      if (windowListeners?.get(type) === handler) windowListeners.delete(type)
    },
    visualViewport: null,
  }
  globalThis.document = {
    activeElement: null,
    visibilityState: 'visible',
    addEventListener(type, handler) { documentListeners?.set(type, handler) },
    removeEventListener(type, handler) {
      if (documentListeners?.get(type) === handler) documentListeners.delete(type)
    },
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

function mountTailController(chatId, overrides = {}) {
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
  const args = {
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
    ...overrides,
  }
  const hook = renderHook(useScrollMode, args)
  return { hook, listeners, scroll, list, assistant, args }
}

function scrollTraceHasEvent(eventName) {
  return globalThis.window.__mobiusChatScrollTrace?.events
    ?.some(({ event }) => event === eventName) || false
}

function installManualTimers() {
  const previousSetTimeout = globalThis.setTimeout
  const previousClearTimeout = globalThis.clearTimeout
  const timers = new Map()
  let timerId = 0
  globalThis.setTimeout = (callback, delay) => {
    timerId += 1
    timers.set(timerId, { callback, delay })
    return timerId
  }
  globalThis.clearTimeout = id => timers.delete(id)
  const pending = delay => [...timers.entries()]
    .filter(([, timer]) => timer.delay === delay)
  const run = ([id, timer]) => {
    timers.delete(id)
    timer.callback()
  }
  return {
    pending,
    run,
    runLatest(delay) {
      const match = pending(delay).at(-1)
      assert.ok(match, `expected a pending ${delay}ms timer`)
      run(match)
    },
    restore() {
      globalThis.setTimeout = previousSetTimeout
      globalThis.clearTimeout = previousClearTimeout
    },
  }
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

test('question response resumes the exact follow intent captured at submit', () => {
  const observers = []
  const restoreBrowser = installBrowserEnvironment({ observers })
  try {
    const { hook, listeners, scroll } = mountTailController('question-follow-owner')
    const target = { parentElement: scroll, closest: () => null }

    listeners.get('wheel')({
      type: 'wheel', deltaY: 80, shiftKey: false, target,
    })
    assert.equal(scroll.dataset.scrollMode, 'FOLLOW_BOTTOM')

    const submission = hook.result.current.freezeQuestionSubmission()
    assert.equal(submission.mode.kind, 'ANCHOR_AT')
    assert.equal(submission.mode.questionSubmitBaseMode.kind, 'FOLLOW_BOTTOM')
    assert.equal(scroll.dataset.scrollMode, 'ANCHOR_AT')

    scroll.clientHeight = 620
    observers[0].callback([{ target: scroll }])
    assert.equal(scroll.dataset.scrollMode, 'ANCHOR_AT',
      'keyboard close cannot release the submission before response activity')

    hook.result.current.resumeQuestionSubmissionOnResponse(submission)
    assert.equal(scroll.dataset.scrollMode, 'FOLLOW_BOTTOM',
      'the first visible continuation restores the captured follow mode')
    hook.unmount()
  } finally {
    restoreBrowser()
  }
})

test('question response cannot restore follow after a newer reader scroll', () => {
  const restoreBrowser = installBrowserEnvironment()
  try {
    const { hook, listeners, scroll } = mountTailController(
      'question-reader-override-owner',
    )
    const target = { parentElement: scroll, closest: () => null }

    listeners.get('wheel')({
      type: 'wheel', deltaY: 80, shiftKey: false, target,
    })
    assert.equal(scroll.dataset.scrollMode, 'FOLLOW_BOTTOM')

    listeners.get('wheel')({
      type: 'wheel', deltaY: -120, shiftKey: false, target,
    })
    scroll.scrollTop -= 120
    listeners.get('scroll')()

    const submission = hook.result.current.freezeQuestionSubmission()
    assert.equal(submission.mode.questionSubmitBaseMode.kind, 'ANCHOR_AT')

    hook.result.current.resumeQuestionSubmissionOnResponse(submission)
    assert.equal(scroll.dataset.scrollMode, 'ANCHOR_AT',
      'new response content cannot revive follow after the reader moved')
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
    // React textarea sizing can commit after the input handler's two-frame
    // caret pass. The ResizeObserver remains the owner of the pre-growth
    // anchor even when its delivery is later than that pass.
    flushFrames()
    observers[0].callback([{
      target: editor,
      borderBoxSize: [{ blockSize: editorHeight }],
    }])
    assert.equal(scroll.scrollTop, beforeGrowth,
      'field growth keeps the question card at its reader-owned coordinate')
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

test('visible FOLLOW_BOTTOM survives repeated Markdown-sized content growth', () => {
  const observers = []
  const restoreBrowser = installBrowserEnvironment({ observers })
  try {
    const { hook, scroll, list, assistant } = mountTailController(
      'markdown-growth-follow',
    )
    hook.result.current.followLatest()
    assert.equal(scroll.dataset.scrollMode, 'FOLLOW_BOTTOM')

    for (const addedHeight of [64, 148, 96, 240]) {
      assistant.offsetHeight += addedHeight
      list.offsetHeight += addedHeight
      scroll.scrollHeight += addedHeight
      observers[0].callback([{ target: list }])
      assert.equal(scroll.dataset.scrollMode, 'FOLLOW_BOTTOM',
        'rendering structure cannot manufacture a reading hold')
      assert.equal(scroll.scrollTop, scroll.scrollHeight - scroll.clientHeight,
        'each visible section or reserved media frame follows the physical tail')
    }

    scroll.clientHeight = 420
    observers[0].callback([{ target: scroll }])
    assert.equal(scroll.dataset.scrollMode, 'FOLLOW_BOTTOM',
      'viewport geometry preserves the semantic mode')
    assert.equal(scroll.scrollTop, scroll.scrollHeight - scroll.clientHeight,
      'the existing follow intent owns the resized physical tail')

    hook.unmount()
  } finally {
    restoreBrowser()
  }
})

test('a hidden chat performs no scroll work and returns to its saved hold', () => {
  const observers = []
  const restoreBrowser = installBrowserEnvironment({ observers })
  try {
    const mounted = mountTailController('background-hold')
    const { hook, scroll, list, assistant, args } = mounted
    hook.result.current.followLatest()
    assert.equal(scroll.dataset.scrollMode, 'FOLLOW_BOTTOM')
    const visibleTop = scroll.scrollTop

    hook.rerender({ ...args, ownsReadingPosition: false })
    assert.equal(observers[0].disconnected, true,
      'the outgoing chat removes its layout observer')
    assert.equal(scroll.dataset.scrollMode, 'ANCHOR_AT',
      'leaving freezes follow into an exact reading coordinate')

    assistant.offsetHeight += 300
    list.offsetHeight += 300
    scroll.scrollHeight += 300
    assert.equal(scroll.scrollTop, visibleTop,
      'content may grow while hidden without moving the retained transcript')

    hook.rerender({ ...args, ownsReadingPosition: true })
    assert.equal(scroll.dataset.scrollMode, 'ANCHOR_AT',
      'return restores a hold and never manufactures follow intent')
    assert.equal(scroll.scrollTop, visibleTop,
      'return preserves the pre-background visible coordinate')

    hook.unmount()
  } finally {
    restoreBrowser()
  }
})

test('an empty chat keeps its first-send pin when the transcript mounts', () => {
  const observers = []
  const restoreBrowser = installBrowserEnvironment({ observers })
  try {
    const scrollRef = { current: null }
    const spacerRef = { current: null }
    const messagesRef = { current: [] }
    const args = {
      chatId: 'empty-first-send',
      scrollRef,
      spacerRef,
      lastUserMsgRef: { current: null },
      chatRef: { current: null },
      footRef: { current: null },
      messages: [],
      messagesRef,
      loadingOlderRef: { current: false },
      initialEntryPhase: 'ready',
      ownsReadingPosition: true,
    }
    const hook = renderHook(useScrollMode, args)
    const intent = hook.result.current.captureSendIntent({ isFirstUserMsg: true })
    hook.result.current.commitSendIntent({ cid: 'first-user', intent })

    const userRow = fakeElement({
      dataset: { cid: 'first-user', key: 'first-user' },
      offsetTop: 120,
      offsetHeight: 40,
    })
    const list = fakeElement({ offsetHeight: 160 })
    const scroll = fakeElement({
      scrollTop: 0,
      scrollHeight: 700,
      clientHeight: 500,
      querySelector(selector) {
        if (selector === '.chat__list') return list
        if (selector.includes('data-cid="first-user"')) return userRow
        return null
      },
      querySelectorAll(selector) {
        if (selector === '.chat__msg--user[data-cid]') return [userRow]
        return []
      },
    })
    scroll.parentElement = fakeElement()
    scrollRef.current = scroll
    spacerRef.current = fakeElement()
    args.lastUserMsgRef.current = userRow
    args.chatRef.current = fakeElement()
    args.footRef.current = fakeElement({ offsetHeight: 80 })
    const messages = [{ role: 'user', cid: 'first-user', content: 'Hello' }]
    messagesRef.current = messages
    hook.rerender({ ...args, messages })

    assert.equal(scroll.scrollTop, userRow.offsetTop - PIN_OFFSET,
      'mounting the first row must apply the already-committed send pin')
    hook.unmount()
  } finally {
    restoreBrowser()
  }
})

test('a same-turn Q&A response start restores prior follow and keeps following growth', () => {
  const observers = []
  const restoreBrowser = installBrowserEnvironment({ observers })
  try {
    const { hook, scroll, list, assistant } = mountTailController(
      'question-follow-handoff',
    )
    hook.result.current.followLatest()
    const submission = hook.result.current.freezeQuestionSubmission()
    assert.equal(scroll.dataset.scrollMode, 'ANCHOR_AT',
      'submitting the answer holds the card through its pending reflow')

    hook.result.current.resumeQuestionSubmissionOnResponse(submission)
    assert.equal(scroll.dataset.scrollMode, 'FOLLOW_BOTTOM',
      'the first post-answer activity restores the follow intent that owned the card')

    assistant.offsetHeight += 180
    list.offsetHeight += 180
    scroll.scrollHeight += 180
    observers[0].callback([{ target: list }])
    assert.equal(scroll.scrollTop, scroll.scrollHeight - scroll.clientHeight,
      'resumed output remains attached to the physical tail')
    hook.unmount()
  } finally {
    restoreBrowser()
  }
})

test('gesture-start diagnostics add no transcript measurement to the hot path', () => {
  const restoreBrowser = installBrowserEnvironment()
  try {
    const { hook, listeners, scroll } = mountTailController(
      'gesture-trace-cost',
    )
    let height = scroll.scrollHeight
    let heightReads = 0
    Object.defineProperty(scroll, 'scrollHeight', {
      configurable: true,
      get() {
        heightReads += 1
        return height
      },
      set(value) { height = value },
    })
    globalThis.window.__mobiusChatScrollTrace = undefined

    const target = { parentElement: scroll, closest: () => null }
    listeners.get('pointerdown')({
      type: 'pointerdown', pointerType: 'mouse', button: 0, target,
    })
    assert.equal(heightReads, 0,
      'claiming reader ownership must not synchronously lay out the transcript')

    scroll.scrollTop -= 20
    listeners.get('scroll')()
    assert.equal(heightReads, 1,
      'the first scroll reads tail geometry once, without a second trace read')
    assert.equal(
      globalThis.window.__mobiusChatScrollTrace.events.at(-1)?.geometry,
      null,
      'hot-path traces remain geometry-free',
    )

    hook.unmount()
  } finally {
    restoreBrowser()
  }
})

test('touch contact survives a controller reinstall and settles only after the last lift', () => {
  const windowListeners = new Map()
  const documentListeners = new Map()
  const restoreBrowser = installBrowserEnvironment({
    windowListeners,
    documentListeners,
  })
  const timers = installManualTimers()

  try {
    const mounted = mountTailController('touch-contact-reinstall')
    const { hook, listeners, scroll, args } = mounted
    const target = { parentElement: scroll, closest: () => null }
    globalThis.window.__mobiusChatScrollTrace = undefined

    listeners.get('touchstart')({ touches: [{}, {}] })
    listeners.get('pointerdown')({
      type: 'pointerdown', pointerType: 'touch', button: 0, clientY: 220, target,
    })
    scroll.scrollTop -= 60
    listeners.get('scroll')()
    listeners.get('scrollend')()
    assert.equal(
      scrollTraceHasEvent('reader:scroll-settled'),
      false,
      'native scrollend cannot hand layout ownership back under live contact',
    )

    const nextMessages = [
      ...args.messages,
      { role: 'assistant', cid: 'assistant-next', content: 'More output' },
    ]
    args.messagesRef.current = nextMessages
    hook.rerender({ ...args, messages: nextMessages })
    assert.equal(typeof windowListeners.get('touchend'), 'function',
      'window lift delivery remains installed after the controller is replaced')

    windowListeners.get('touchend')({ touches: [{}] })
    assert.equal(
      scrollTraceHasEvent('reader:scroll-settled'),
      false,
      'lifting one of several fingers keeps reader ownership active',
    )
    windowListeners.get('touchend')({ touches: [] })
    timers.runLatest(250)
    assert.equal(
      scrollTraceHasEvent('reader:scroll-settled'),
      true,
      'the quiet edge starts from the final lift and settles the inherited gesture',
    )

    hook.unmount()
  } finally {
    timers.restore()
    restoreBrowser()
  }
})

test('the no-scroll safety cap re-arms while a finger remains down', () => {
  const frames = []
  const restoreBrowser = installBrowserEnvironment({ frames })
  const timers = installManualTimers()

  try {
    const { hook, listeners, scroll } = mountTailController('touch-no-scroll-cap')
    const target = { parentElement: scroll, closest: () => null }
    globalThis.window.__mobiusChatScrollTrace = undefined

    listeners.get('touchstart')({ touches: [{}] })
    listeners.get('pointerdown')({
      type: 'pointerdown', pointerType: 'touch', button: 0, clientY: 220, target,
    })
    const firstCap = timers.pending(2000).at(-1)
    assert.ok(firstCap, 'a no-scroll contact has one bounded safety cap')
    timers.run(firstCap)
    assert.ok(timers.pending(2000).length > 0,
      'the cap re-arms instead of releasing live contact')
    assert.equal(
      scrollTraceHasEvent('reader:no-scroll-release'),
      false,
    )

    listeners.get('touchend')({ touches: [] })
    assert.ok(frames.length > 0, 'the final lift schedules the ordinary frame handoff')
    while (frames.length) frames.shift()()
    assert.equal(
      scrollTraceHasEvent('reader:no-scroll-release'),
      true,
      'the no-scroll gesture releases only after contact ends',
    )
    hook.unmount()
  } finally {
    timers.restore()
    restoreBrowser()
  }
})

test('backgrounding clears a wedged contact and settles its dirty gesture', () => {
  const documentListeners = new Map()
  const restoreBrowser = installBrowserEnvironment({ documentListeners })
  try {
    const { hook, listeners, scroll } = mountTailController(
      'touch-visibility-wedge',
    )
    const target = { parentElement: scroll, closest: () => null }
    globalThis.window.__mobiusChatScrollTrace = undefined

    listeners.get('touchstart')({ touches: [{}] })
    listeners.get('pointerdown')({
      type: 'pointerdown', pointerType: 'touch', button: 0, clientY: 220, target,
    })
    scroll.scrollTop -= 40
    listeners.get('scroll')()
    globalThis.document.visibilityState = 'hidden'
    documentListeners.get('visibilitychange')()
    assert.equal(
      scrollTraceHasEvent('reader:scroll-settled'),
      true,
      'a lost lift cannot freeze layout ownership after the page is hidden',
    )
    hook.unmount()
  } finally {
    restoreBrowser()
  }
})

test('stream and row churn cannot restart the absolute reveal deadline', () => {
  const restoreBrowser = installBrowserEnvironment()
  const previousSetTimeout = globalThis.setTimeout
  const previousClearTimeout = globalThis.clearTimeout
  let timerId = 0
  let revealDeadlines = 0
  globalThis.setTimeout = (_callback, delay) => {
    if (delay === 1500) revealDeadlines += 1
    timerId += 1
    return timerId
  }
  globalThis.clearTimeout = () => {}
  try {
    const mounted = mountTailController('fixed-reveal-deadline')
    const { hook, args } = mounted
    assert.equal(revealDeadlines, 1)

    const streamed = args.messages.map(message => ({
      ...message,
      content: `${message.content}\n## A streamed section`,
    }))
    args.messagesRef.current = streamed
    hook.rerender({ ...args, messages: streamed })
    assert.equal(revealDeadlines, 1,
      'content-only streaming must not move the safety cap')

    const nextRow = [
      ...streamed,
      { role: 'assistant', cid: 'assistant-next', content: 'More output' },
    ]
    args.messagesRef.current = nextRow
    hook.rerender({ ...args, messages: nextRow })
    assert.equal(revealDeadlines, 1,
      'even a structural row commit keeps the mount-scoped deadline')
    hook.unmount()
  } finally {
    globalThis.setTimeout = previousSetTimeout
    globalThis.clearTimeout = previousClearTimeout
    restoreBrowser()
  }
})
