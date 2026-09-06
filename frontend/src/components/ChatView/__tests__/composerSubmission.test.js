import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { hasSendablePayload } from '../composerSubmission.js'

test('plain text remains sendable without an attachment', () => {
  assert.equal(hasSendablePayload('hello', []), true)
})

test('a completed attachment is sendable without text', () => {
  assert.equal(hasSendablePayload('', [{
    name: 'photo.png',
    status: 'done',
  }]), true)
})

test('queued attachment metadata is sendable without a live upload status', () => {
  assert.equal(hasSendablePayload('   ', [{
    name: 'notes.pdf',
    mime_type: 'application/pdf',
  }]), true)
})

test('uploading, failed, and malformed attachment-only drafts are not sendable', () => {
  assert.equal(hasSendablePayload('', [{ name: 'photo.png', status: 'uploading' }]), false)
  assert.equal(hasSendablePayload('', [{ name: 'photo.png', status: 'error' }]), false)
  assert.equal(hasSendablePayload('', [{ status: 'done' }]), false)
})

test('an empty draft remains unsendable', () => {
  assert.equal(hasSendablePayload(' \n ', []), false)
})

test('sendability is decided before submit-time UI and scroll side effects', () => {
  const source = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  const start = source.indexOf('const doSend = useCallback')
  const end = source.indexOf('\n  }, [', start)
  const doSend = source.slice(start, end)

  const validation = doSend.indexOf('if (!hasSendablePayload(text, attachments)) return')
  assert.ok(validation >= 0)
  for (const sideEffect of [
    'setSendFailure(null)',
    'stopVoiceRef.current?.()',
    'captureSendIntent({',
    'freezeQueuedSubmission()',
    'inputRef.current?.blur()',
  ]) {
    assert.ok(
      validation < doSend.indexOf(sideEffect),
      `payload validation must precede ${sideEffect}`,
    )
  }
})

test('failed recovery records the exact content passed by both send paths', () => {
  const source = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  const start = source.indexOf('const doSend = useCallback')
  const end = source.indexOf('\n  }, [', start)
  const doSend = source.slice(start, end)
  const queueSend = doSend.indexOf('queueRequest = sendAfterSettingsSaved(\n          text,')
  const queueRecovery = doSend.indexOf('transportContent: text,', queueSend)
  const contextSend = doSend.indexOf('const result = await sendAfterSettingsSaved(\n        sendText,')
  const contextRecovery = doSend.indexOf('transportContent: sendText,', contextSend)

  assert.ok(queueSend >= 0 && queueRecovery > queueSend && queueRecovery < contextSend,
    'queued/raw recovery must record the raw content passed to transport')
  assert.ok(contextSend >= 0 && contextRecovery > contextSend,
    'fresh/context recovery must record the augmented content passed to transport')
})
