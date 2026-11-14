import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import useVoiceInput from '../../useVoiceInput.js'

function speechResult(transcript, { final = false, confidence = 1 } = {}) {
  const result = {
    0: { transcript, confidence },
    isFinal: final,
  }
  return { resultIndex: 0, results: [result] }
}

function installSpeechRecognition() {
  const instances = []
  class Recognition {
    constructor() {
      this.aborted = false
      instances.push(this)
    }
    start() { this.started = true }
    abort() { this.aborted = true }
  }
  globalThis.window = { SpeechRecognition: Recognition }
  return instances
}

test('live dictation delegates geometry to the controlled composer value owner', () => {
  const instances = installSpeechRecognition()
  let closestCalls = 0
  const input = {
    value: '',
    scrollHeight: 120,
    style: {},
    closest: () => { closestCalls += 1 },
  }
  const transcripts = []
  try {
    const { result } = renderHook(() => useVoiceInput({
      onTranscript: text => transcripts.push(text),
      inputRef: { current: input },
    }))
    result.current.startVoice()
    instances[0].onresult(speechResult('a sufficiently long dictated message'))

    assert.equal(transcripts.at(-1), 'a sufficiently long dictated message')
    assert.equal(input.style.height, undefined)
    assert.equal(closestCalls, 0, 'the hook must not force a second layout')
  } finally {
    delete globalThis.window
  }
})

test('a microphone denial explains browser settings but stays silent in the phone app', () => {
  for (const native of [false, true]) {
    const instances = installSpeechRecognition()
    if (native) globalThis.window.MobiusNative = {}
    const transcripts = []
    try {
      const { result } = renderHook(() => useVoiceInput({
        onTranscript: text => transcripts.push(text),
        inputRef: { current: { value: 'draft' } },
      }))
      result.current.startVoice()
      instances[0].onerror({ error: 'not-allowed' })

      assert.equal(result.current.listeningRef.current, false)
      if (native) {
        assert.deepEqual(transcripts, [], 'the OS prompt was the answer; leave the draft alone')
      } else {
        assert.match(transcripts.at(-1), /^draft \[Microphone access denied/)
      }
    } finally {
      delete globalThis.window
    }
  }
})

test('a manual edit while listening survives late speech results and rebases dictation', () => {
  const instances = installSpeechRecognition()
  const transcripts = []
  const timers = new Map()
  let timerId = 0
  const prevTimeout = globalThis.setTimeout
  const prevClearTimeout = globalThis.clearTimeout
  globalThis.setTimeout = fn => {
    const id = ++timerId
    timers.set(id, fn)
    return id
  }
  globalThis.clearTimeout = id => timers.delete(id)
  try {
    const input = { value: 'draft', style: {}, scrollHeight: 30, closest: () => null }
    const { result } = renderHook(() => useVoiceInput({
      onTranscript: text => transcripts.push(text),
      inputRef: { current: input },
    }))
    result.current.startVoice()
    const retired = instances[0]

    result.current.acceptManualEdit('draft corrected')
    assert.equal(retired.aborted, true)

    retired.onresult(speechResult(' stale interim'))
    assert.deepEqual(transcripts, [], 'an abort-race result cannot overwrite the edit')

    assert.equal(timers.size, 1)
    timers.values().next().value()
    const resumed = instances[1]
    resumed.onresult(speechResult('and more', { final: true }))
    assert.equal(transcripts.at(-1), 'draft corrected and more')
  } finally {
    globalThis.setTimeout = prevTimeout
    globalThis.clearTimeout = prevClearTimeout
    delete globalThis.window
  }
})

test('dictation holds the recording audio session only while listening', () => {
  installSpeechRecognition()
  const previousNavigator = Object.getOwnPropertyDescriptor(globalThis, 'navigator')
  const audioSession = { type: 'auto' }
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: { audioSession } })
  try {
    const { result, unmount } = renderHook(() => useVoiceInput({
      onTranscript: () => {},
      inputRef: { current: { value: '' } },
    }))
    result.current.startVoice()
    assert.equal(audioSession.type, 'play-and-record')
    result.current.stopVoice()
    assert.equal(audioSession.type, 'auto')

    result.current.startVoice()
    unmount()
    assert.equal(audioSession.type, 'auto')
  } finally {
    Object.defineProperty(globalThis, 'navigator', previousNavigator)
    delete globalThis.window
  }
})

test('a speech service that refuses this browser stops dictation with a note instead of spinning', () => {
  const instances = installSpeechRecognition()
  const transcripts = []
  try {
    const { result } = renderHook(() => useVoiceInput({
      onTranscript: text => transcripts.push(text),
      inputRef: { current: { value: 'draft' } },
    }))
    result.current.startVoice()
    instances[0].onerror({ error: 'network' })
    instances[0].onend()

    assert.equal(result.current.listeningRef.current, false)
    assert.equal(instances.length, 1, 'no restart after a refusal')
    assert.match(transcripts.at(-1), /^draft \[Dictation couldn't reach the speech service\]$/)
  } finally {
    delete globalThis.window
  }
})

function installNativeDictation() {
  const sessions = []
  const native = {
    stops: 0,
    startDictation(listener) {
      sessions.push(listener)
      return Promise.resolve(true)
    },
    stopDictation() {
      native.stops += 1
      return Promise.resolve(true)
    },
  }
  // The app's dictation wins even where Web Speech exists but can't work.
  installSpeechRecognition()
  globalThis.window.MobiusNative = native
  return { native, sessions }
}

test("the app's own dictation writes after the draft and stops through the app", () => {
  const { native, sessions } = installNativeDictation()
  const transcripts = []
  try {
    const { result } = renderHook(() => useVoiceInput({
      onTranscript: text => transcripts.push(text),
      inputRef: { current: { value: 'draft  ' } },
    }))
    result.current.startVoice()
    assert.equal(sessions.length, 1)
    sessions[0]({ type: 'text', text: 'hello' })
    sessions[0]({ type: 'text', text: 'Hello, world.' })
    assert.deepEqual(transcripts, ['draft hello', 'draft Hello, world.'])

    result.current.stopVoice()
    assert.equal(native.stops, 1)
    sessions[0]({ type: 'text', text: 'late words' })
    assert.equal(transcripts.length, 2, 'a stopped session writes nothing')
  } finally {
    delete globalThis.window
  }
})

test("a manual edit restarts the app's dictation from the edited draft", () => {
  const { native, sessions } = installNativeDictation()
  const transcripts = []
  const timers = new Map()
  let timerId = 0
  const prevTimeout = globalThis.setTimeout
  const prevClearTimeout = globalThis.clearTimeout
  globalThis.setTimeout = fn => {
    const id = ++timerId
    timers.set(id, fn)
    return id
  }
  globalThis.clearTimeout = id => timers.delete(id)
  try {
    const { result } = renderHook(() => useVoiceInput({
      onTranscript: text => transcripts.push(text),
      inputRef: { current: { value: '' } },
    }))
    result.current.startVoice()
    sessions[0]({ type: 'text', text: 'first try' })
    result.current.acceptManualEdit('first try, fixed')
    assert.equal(native.stops, 1)
    sessions[0]({ type: 'text', text: 'first try stale' })
    assert.equal(transcripts.at(-1), 'first try')

    timers.values().next().value()
    sessions[1]({ type: 'text', text: 'and more' })
    assert.equal(transcripts.at(-1), 'first try, fixed and more')
  } finally {
    globalThis.setTimeout = prevTimeout
    globalThis.clearTimeout = prevClearTimeout
    delete globalThis.window
  }
})

test("when the app's dictation ends by itself, only a fixable reason leaves a note", async () => {
  for (const [error, note] of [
    ['blocked', /^draft \[Dictation is off for Möbius/],
    ['failed', /^draft \[Dictation stopped/],
    ['declined', null],
    [null, null],
  ]) {
    const { sessions } = installNativeDictation()
    const transcripts = []
    try {
      const { result } = renderHook(() => useVoiceInput({
        onTranscript: text => transcripts.push(text),
        inputRef: { current: { value: 'draft' } },
      }))
      result.current.startVoice()
      sessions[0]({ type: 'end', error })

      assert.equal(result.current.listeningRef.current, false)
      if (note) assert.match(transcripts.at(-1), note)
      else assert.deepEqual(transcripts, [])
    } finally {
      delete globalThis.window
    }
  }
})

test("an app that can't start dictation turns the mic back off", async () => {
  const { native } = installNativeDictation()
  native.startDictation = () => Promise.reject(new Error('not allowed'))
  const transcripts = []
  try {
    const { result } = renderHook(() => useVoiceInput({
      onTranscript: text => transcripts.push(text),
      inputRef: { current: { value: '' } },
    }))
    result.current.startVoice()
    await new Promise(resolve => setImmediate(resolve))
    assert.equal(result.current.listeningRef.current, false)
    assert.match(transcripts.at(-1), /^\[Dictation stopped/)
  } finally {
    delete globalThis.window
  }
})

// Like the desktop app: one dictation session, one listener. Starting again
// replaces the listener; stopping ends the session and drops it.
function installSingleNativeSession() {
  const native = {
    listener: null,
    stops: 0,
    startDictation(listener) {
      native.listener = listener
      return Promise.resolve(true)
    },
    stopDictation() {
      native.stops += 1
      native.listener = null
      return Promise.resolve(true)
    },
    say(text) { native.listener?.({ type: 'text', text }) },
  }
  globalThis.window = { MobiusNative: native }
  return native
}

function renderTwoPanes() {
  const drafts = { a: [], b: [] }
  const rendered = renderHook(() => ({
    a: useVoiceInput({ onTranscript: t => drafts.a.push(t), inputRef: { current: { value: '' } } }),
    b: useVoiceInput({ onTranscript: t => drafts.b.push(t), inputRef: { current: { value: '' } } }),
  }))
  return { ...rendered, drafts }
}

test("starting the app's dictation in another chat moves it there, and the first chat can't stop it", () => {
  const native = installSingleNativeSession()
  try {
    const { result, drafts } = renderTwoPanes()
    result.current.a.startVoice()
    native.say('for a')
    result.current.b.startVoice()
    assert.equal(result.current.a.listening, false, 'the first chat stops showing the mic as on')
    assert.equal(result.current.b.listening, true)

    result.current.a.stopVoice()
    assert.equal(native.stops, 0, "the first chat's stop leaves the session running")
    native.say('for b')
    assert.deepEqual(drafts, { a: ['for a'], b: ['for b'] })

    result.current.b.stopVoice()
    assert.equal(native.stops, 1)
  } finally {
    delete globalThis.window
  }
})

test("closing a chat whose dictation moved elsewhere leaves the other chat listening", () => {
  const native = installSingleNativeSession()
  try {
    const { result, unmount } = renderTwoPanes()
    result.current.a.startVoice()
    result.current.b.startVoice()
    result.current.a.stopVoice()
    assert.equal(native.stops, 0)
    // The shim unmounts both panes, the first one first: only the owner stops.
    unmount()
    assert.equal(native.stops, 1)
  } finally {
    delete globalThis.window
  }
})

test("another chat starting during a manual-edit pause takes over and the pause doesn't take it back", () => {
  const native = installSingleNativeSession()
  const timers = new Map()
  let timerId = 0
  const prevTimeout = globalThis.setTimeout
  const prevClearTimeout = globalThis.clearTimeout
  globalThis.setTimeout = fn => {
    const id = ++timerId
    timers.set(id, fn)
    return id
  }
  globalThis.clearTimeout = id => timers.delete(id)
  try {
    const { result, drafts } = renderTwoPanes()
    result.current.a.startVoice()
    result.current.a.acceptManualEdit('edited')
    assert.equal(timers.size, 1)
    result.current.b.startVoice()
    assert.equal(timers.size, 0, "the first chat's restart is cancelled")
    assert.equal(result.current.a.listening, false)

    native.say('for b')
    assert.deepEqual(drafts.b, ['for b'])
  } finally {
    globalThis.setTimeout = prevTimeout
    globalThis.clearTimeout = prevClearTimeout
    delete globalThis.window
  }
})
