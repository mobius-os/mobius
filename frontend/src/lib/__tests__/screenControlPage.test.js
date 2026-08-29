import test from 'node:test'
import assert from 'node:assert/strict'

import {
  boundedScreenCaptureSize,
  createScreenControlClient,
  isSensitiveScreenControlField,
  parseScreenControlFrameRef,
  screenControlCommandExpired,
} from '../screenControlPage.js'


test('live screen refs remain exact-frame and reject ambiguous shapes', () => {
  assert.deepEqual(parseScreenControlFrameRef('app:42:e7'), {
    appId: '42', ref: 'e7',
  })
  assert.equal(parseScreenControlFrameRef('e7'), null)
  assert.equal(parseScreenControlFrameRef('app:42:button'), null)
  assert.equal(parseScreenControlFrameRef('app:42:e7:extra'), null)
})


test('credential and payment fields stay sensitive while ordinary text does not', () => {
  assert.equal(isSensitiveScreenControlField('password', ''), true)
  assert.equal(isSensitiveScreenControlField('text', 'section-login current-password'), true)
  assert.equal(isSensitiveScreenControlField('text', 'one-time-code'), true)
  assert.equal(isSensitiveScreenControlField('text', 'cc-number'), true)
  assert.equal(isSensitiveScreenControlField('email', 'email'), false)
})


test('shared screenshots preserve aspect ratio under the response-size ceiling', () => {
  assert.deepEqual(boundedScreenCaptureSize(1440, 900), { width: 1440, height: 900 })
  assert.deepEqual(boundedScreenCaptureSize(5120, 2880), { width: 2560, height: 1440 })
  assert.deepEqual(boundedScreenCaptureSize(1170, 2532), { width: 1170, height: 2532 })
  assert.equal(boundedScreenCaptureSize(0, 900), null)
})


test('browser command deadlines reject only commands whose start window passed', () => {
  assert.equal(screenControlCommandExpired(1000, 999), false)
  assert.equal(screenControlCommandExpired(1000, 1000), true)
  assert.equal(screenControlCommandExpired(undefined, 1000), false)
})


test('a capture that ended before client attachment is reconciled immediately', async () => {
  const realFetch = globalThis.fetch
  const calls = []
  globalThis.fetch = async (_url, options = {}) => {
    calls.push(options.method || 'GET')
    if (options.method === 'DELETE') return { ok: true }
    return new Promise((_resolve, reject) => {
      options.signal?.addEventListener('abort', () => {
        reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
      }, { once: true })
    })
  }
  const track = {
    readyState: 'ended',
    addEventListener() {},
    stop() {},
  }
  const capture = {
    stream: {
      getVideoTracks: () => [track],
      getTracks: () => [track],
    },
    video: { srcObject: {} },
  }
  const ended = []
  try {
    createScreenControlClient({
      sessionId: 'session-1',
      expiresAt: Date.now() + 60_000,
      capture,
      onEnded: reason => ended.push(reason),
    })
    await new Promise(resolve => setImmediate(resolve))

    assert.deepEqual(ended, ['stopped'])
    assert.equal(capture.video.srcObject, null)
    assert.deepEqual(calls, ['GET', 'DELETE'])
  } finally {
    globalThis.fetch = realFetch
  }
})


test('browser capture retires at the local copy of the consent deadline', async () => {
  const realFetch = globalThis.fetch
  globalThis.fetch = async (_url, options = {}) => {
    if (options.method === 'DELETE') return { ok: true }
    return new Promise((_resolve, reject) => {
      options.signal?.addEventListener('abort', () => {
        reject(Object.assign(new Error('aborted'), { name: 'AbortError' }))
      }, { once: true })
    })
  }
  const track = {
    readyState: 'live',
    addEventListener() {},
    stop() { this.readyState = 'ended' },
  }
  const capture = {
    stream: {
      getVideoTracks: () => [track],
      getTracks: () => [track],
    },
    video: { srcObject: {} },
  }
  const ended = []
  try {
    createScreenControlClient({
      sessionId: 'session-1',
      expiresAt: Date.now() - 1,
      capture,
      onEnded: reason => ended.push(reason),
    })
    await new Promise(resolve => setTimeout(resolve, 5))

    assert.deepEqual(ended, ['expired'])
    assert.equal(track.readyState, 'ended')
  } finally {
    globalThis.fetch = realFetch
  }
})
