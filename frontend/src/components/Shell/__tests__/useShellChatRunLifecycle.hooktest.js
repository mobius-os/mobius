import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from '../../ChatView/hooks/__tests__/react-hook-shim.mjs'
import useShellChatRunLifecycle from '../useShellChatRunLifecycle.js'


test('mounted and durable runs share one projected streaming set', () => {
  const { result, rerender } = renderHook(useShellChatRunLifecycle, [
    { id: 'durable', running: true },
  ])

  result.current.markStreamingStart('local')
  assert.deepEqual(
    [...result.current.streamingChatIds].sort(),
    ['durable', 'local'],
  )
  assert.equal(
    result.current.streamingChatIdsRef.current.has('local'),
    true,
    'same-task decisions see a mounted start before React commit timing matters',
  )

  rerender([{ id: 'durable', running: false }])
  assert.deepEqual([...result.current.streamingChatIds], ['local'])
})


test('a stale settled row cannot retire an unacknowledged local start', () => {
  const { result } = renderHook(useShellChatRunLifecycle, [])
  result.current.markStreamingStart('chat-1')

  const retained = result.current.reconcileLocalChatRuns([
    { id: 'chat-1', running: false },
  ], new Set())

  assert.equal(retained.has('chat-1'), true)
  assert.equal(result.current.streamingChatIds.has('chat-1'), true)
})


test('fresh durable settlement retires an acknowledged hidden run', () => {
  const { result } = renderHook(useShellChatRunLifecycle, [])
  result.current.markStreamingAcknowledged('chat-1')

  const settled = result.current.reconcileLocalChatRuns([
    { id: 'chat-1', running: false },
  ], new Set())

  assert.equal(settled.has('chat-1'), false)
  assert.equal(result.current.streamingChatIds.has('chat-1'), false)
})


test('a visible chat keeps ownership until its mounted stream settles', () => {
  const { result } = renderHook(useShellChatRunLifecycle, [])
  result.current.markStreamingAcknowledged('chat-1')

  const protectedRun = result.current.reconcileLocalChatRuns([
    { id: 'chat-1', running: false },
  ], new Set(['chat-1']))
  assert.equal(protectedRun.has('chat-1'), true)

  const settled = result.current.reconcileLocalChatRuns([
    { id: 'chat-1', running: false },
  ], new Set())
  assert.equal(settled.has('chat-1'), false)
})


test('start and finish remain observable when one render ends idle', () => {
  const { result } = renderHook(useShellChatRunLifecycle, [])

  result.current.markChatRunActivity('chat-1')
  result.current.markChatRunFinished('chat-1')

  assert.deepEqual(result.current.chatRunSignalFor('chat-1'), {
    seq: 2,
    starts: 1,
    finishes: 1,
  })
  assert.deepEqual(
    result.current.chatRunSignalFor('chat-2'),
    { seq: 0, starts: 0, finishes: 0 },
    'another pane does not inherit this chat activity',
  )
})
