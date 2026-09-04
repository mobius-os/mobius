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

test('fresh-send transcript reconciliation starts only after acknowledgement', () => {
  const source = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
  const start = source.indexOf('// FRESH SEND PATH: no active turn, no queue.')
  const end = source.indexOf('\n  }, [', start)
  const freshSend = source.slice(start, end)
  const transport = freshSend.indexOf('const result = await sendAfterSettingsSaved(')
  const canonicalCommit = freshSend.indexOf(
    'replaceOptimisticWithBatch(prev, cid, startedMessages)',
    transport,
  )
  const reconcile = freshSend.indexOf('void fetchMessages({ force: true,', transport)

  assert.ok(transport >= 0 && canonicalCommit > transport,
    'the accepted server row must replace its optimistic twin after acknowledgement')
  assert.ok(reconcile > canonicalCommit,
    'a compact snapshot must not reconcile before the send is acknowledged and canonical')
  assert.match(
    freshSend.slice(reconcile, reconcile + 120),
    /preserveAcceptedCid:\s*cid/,
    'the handoff must retain the accepted row until the compact snapshot proves its cid',
  )
  assert.equal(
    freshSend.slice(0, transport).includes('fetchMessages({ force: true })'),
    false,
    'an idle pre-ack snapshot must never erase the optimistic turn',
  )
})
