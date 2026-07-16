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

test('live dictation grows to the composer cap and bottom-anchors its mic', () => {
  const instances = installSpeechRecognition()
  const toggles = []
  const input = {
    value: '',
    scrollHeight: 120,
    style: {},
    closest: () => ({
      classList: { toggle: (...args) => toggles.push(args) },
    }),
  }
  const transcripts = []
  const prevRaf = globalThis.requestAnimationFrame
  globalThis.requestAnimationFrame = fn => { fn(); return 1 }
  try {
    const { result } = renderHook(() => useVoiceInput({
      onTranscript: text => transcripts.push(text),
      inputRef: { current: input },
    }))
    result.current.startVoice()
    instances[0].onresult(speechResult('a sufficiently long dictated message'))

    assert.equal(transcripts.at(-1), 'a sufficiently long dictated message')
    assert.equal(input.style.height, '120px')
    assert.deepEqual(toggles.at(-1), ['chat__pill--tall', true])
  } finally {
    globalThis.requestAnimationFrame = prevRaf
    delete globalThis.window
  }
})

test('a manual edit while listening survives late speech results and rebases dictation', () => {
  const instances = installSpeechRecognition()
  const transcripts = []
  const timers = new Map()
  let timerId = 0
  const prevTimeout = globalThis.setTimeout
  const prevClearTimeout = globalThis.clearTimeout
  const prevRaf = globalThis.requestAnimationFrame
  globalThis.setTimeout = fn => {
    const id = ++timerId
    timers.set(id, fn)
    return id
  }
  globalThis.clearTimeout = id => timers.delete(id)
  globalThis.requestAnimationFrame = fn => { fn(); return 1 }
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
    globalThis.requestAnimationFrame = prevRaf
    delete globalThis.window
  }
})
