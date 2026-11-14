import { test } from 'node:test'
import assert from 'node:assert/strict'

import { endVoicePromptOnPause } from '../speech/pocketTtsClonePrompt.js'

const RATE = 24_000
const FRAME = 480
const PAUSE = 0.08 * RATE

function tone(seconds, amplitude) {
  return Float32Array.from({ length: Math.round(seconds * RATE) }, (_, index) => (
    amplitude * Math.sin(index * 0.2)
  ))
}

function joined(...parts) {
  const out = new Float32Array(parts.reduce((total, part) => total + part.length, 0))
  let offset = 0
  for (const part of parts) {
    out.set(part, offset)
    offset += part.length
  }
  return out
}

test('a cloned voice prompt drops trailing silence and ends on exactly 80 ms of zeros', () => {
  const speech = tone(1, 0.5)
  const prompt = endVoicePromptOnPause(joined(speech, new Float32Array(RATE)), RATE)

  assert.equal(prompt.length, speech.length + PAUSE)
  assert.ok(prompt.subarray(speech.length).every((value) => value === 0))
})

test('a cloned voice prompt fades its last 20 ms of kept audio down to silence', () => {
  const prompt = endVoicePromptOnPause(new Float32Array(RATE).fill(0.5), RATE)
  const fade = 0.02 * RATE

  assert.equal(prompt[RATE - fade - 1], 0.5)
  assert.ok(Math.abs(prompt[RATE - fade] - 0.5) < 1e-6)
  assert.equal(prompt[RATE - 1], 0)
})

test('a cloned voice prompt keeps trailing sound within 35 dB of its loudest frame, as upstream does', () => {
  const speech = tone(1, 0.5)
  const roomNoise = tone(0.5, 0.5 / 20) // -26 dB: audible noise, not a pause
  const prompt = endVoicePromptOnPause(joined(speech, roomNoise), RATE)

  assert.equal(prompt.length, speech.length + roomNoise.length + PAUSE)
})

test('a cloned voice prompt trims on whole 20 ms frames without changing the caller recording', () => {
  const recording = joined(tone(1.01, 0.5), new Float32Array(FRAME * 10))
  const before = Float32Array.from(recording)

  const prompt = endVoicePromptOnPause(recording, RATE)

  assert.equal((prompt.length - PAUSE) % FRAME, 0)
  assert.deepEqual(recording, before)
})
