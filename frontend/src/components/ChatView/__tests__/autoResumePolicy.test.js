import { test } from 'node:test'
import assert from 'node:assert/strict'

import {
  AUTO_RESUME_REQUEST_TIMEOUT_MS,
  MAX_TIMER_DELAY_MS,
  resetDeadlineDelay,
  resetDeadlineState,
  saveAutoResumePolicy,
  saveRestartResumePolicy,
} from '../autoResumePolicy.js'


function jsonResponse(data, { ok = true, status = 200 } = {}) {
  return {
    ok,
    status,
    async json() { return data },
  }
}


test('policy PATCH is time-boxed and returns the authoritative response', async () => {
  const calls = []
  const result = await saveAutoResumePolicy({
    chatId: 'chat/a',
    next: true,
    request: async (...args) => {
      calls.push(args)
      return jsonResponse({ auto_resume_on_limit: true })
    },
  })

  assert.deepEqual(result, { value: true, error: '' })
  assert.equal(calls.length, 1)
  assert.equal(calls[0][0], '/chats/chat%2Fa')
  assert.equal(calls[0][1].timeoutMs, AUTO_RESUME_REQUEST_TIMEOUT_MS)
})


test('restart policy uses a separate explicit wire field', async () => {
  const calls = []
  const result = await saveRestartResumePolicy({
    chatId: 'chat-1',
    next: true,
    request: async (path, options) => {
      calls.push({ path, options })
      return jsonResponse({
        auto_resume_on_limit: true,
        auto_resume_on_restart: true,
      })
    },
  })

  assert.deepEqual(result, { value: true, error: '' })
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    auto_resume_on_restart: true,
  })
})


test('lost PATCH response reconciles server truth before updating the switch', async () => {
  let committed = false
  const calls = []
  const result = await saveAutoResumePolicy({
    chatId: 'chat-1',
    next: true,
    request: async (path, options) => {
      calls.push({ path, options })
      if (options.method === 'PATCH') {
        committed = true
        throw new Error('response lost')
      }
      return jsonResponse({ auto_resume_on_limit: committed })
    },
  })

  assert.deepEqual(result, { value: true, error: '' })
  assert.equal(calls.length, 2)
  assert.equal(calls[1].path, '/chats/chat-1?limit=1')
  assert.equal(calls[1].options.timeoutMs, AUTO_RESUME_REQUEST_TIMEOUT_MS)
})


test('failed PATCH reports an error after GET confirms the old value', async () => {
  const result = await saveAutoResumePolicy({
    chatId: 'chat-1',
    next: true,
    request: async (_path, options) => {
      if (options.method === 'PATCH') {
        return jsonResponse({ detail: 'not saved' }, { ok: false, status: 503 })
      }
      return jsonResponse({ auto_resume_on_limit: false })
    },
  })

  assert.deepEqual(result, { value: false, error: 'not saved' })
})


test('double network failure explicitly marks policy state as unverified', async () => {
  const result = await saveAutoResumePolicy({
    chatId: 'chat-1',
    next: false,
    request: async () => { throw new Error('offline') },
  })

  assert.equal(result.value, null)
  assert.match(result.error, /offline.*could not be verified/i)
})


test('timeout failures use actionable copy after authoritative reconciliation', async () => {
  const timeout = new Error('This operation was aborted')
  timeout.name = 'AbortError'
  const result = await saveAutoResumePolicy({
    chatId: 'chat-1',
    next: true,
    request: async (_path, options) => {
      if (options.method === 'PATCH') throw timeout
      return jsonResponse({ auto_resume_on_limit: false })
    },
  })

  assert.equal(result.value, false)
  assert.match(result.error, /request timed out/i)
})


test('deadline state derives immediately from the current timestamp', () => {
  const now = Date.parse('2030-01-01T00:00:00Z')
  assert.deepEqual(
    resetDeadlineState('2029-12-31T23:59:59Z', now),
    { elapsed: true, remainingMs: -1000 },
  )
  assert.deepEqual(
    resetDeadlineState('2030-01-01T00:00:01Z', now),
    { elapsed: false, remainingMs: 1000 },
  )
})


test('long deadlines wake at the timer ceiling without becoming elapsed', () => {
  const now = Date.parse('2030-01-01T00:00:00Z')
  const farFuture = new Date(now + MAX_TIMER_DELAY_MS + 60_000).toISOString()
  assert.equal(resetDeadlineDelay(farFuture, now), MAX_TIMER_DELAY_MS)
  assert.equal(resetDeadlineState(farFuture, now).elapsed, false)
})
