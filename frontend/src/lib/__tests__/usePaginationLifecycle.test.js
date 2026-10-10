import test from 'node:test'
import assert from 'node:assert/strict'

import { renderHook } from '../../components/ChatView/hooks/__tests__/react-hook-shim.mjs'
import usePaginationLifecycle from '../../components/ChatView/usePaginationLifecycle.js'


function args(chatId, refs) {
  return {
    chatId,
    hidden: false,
    loadNonce: 0,
    provisionalNewChat: false,
    searchAnchorKey: null,
    searchRevealId: null,
    loadingOlderRef: refs.loading,
    pageRef: refs.page,
    followupRafRef: refs.followup,
    retryRef: refs.retry,
  }
}


test('a chat-switch layout commit retires old pagination before passive cleanup', () => {
  const previousCancel = globalThis.cancelAnimationFrame
  const previousClearTimeout = globalThis.clearTimeout
  const cancelled = []
  const clearedTimers = []
  globalThis.cancelAnimationFrame = frame => cancelled.push(frame)
  globalThis.clearTimeout = timer => clearedTimers.push(timer)
  try {
    const refs = {
      loading: { current: true },
      page: { current: null },
      followup: { current: 41 },
      retry: { current: { timer: 0, attempts: 0 } },
    }
    const hook = renderHook(usePaginationLifecycle, args('old-chat', refs))
    const capturedLifecycle = hook.result.current.current
    const oldResponseIsCurrent = () => (
      hook.result.current.current === capturedLifecycle
    )
    assert.equal(oldResponseIsCurrent(), true)

    // renderHook flushes layout cleanup/setup as part of this rerender commit.
    // No passive activation cleanup is needed to invalidate the old response.
    refs.loading.current = true
    refs.followup.current = 42
    refs.retry.current = { timer: 77, attempts: 3 }
    hook.rerender(args('new-chat', refs))

    assert.equal(oldResponseIsCurrent(), false,
      'the retired response cannot publish rows into the new transcript')
    assert.equal(refs.loading.current, false)
    assert.equal(refs.followup.current, 0)
    assert.deepEqual(cancelled, [42])
    assert.deepEqual(clearedTimers, [77],
      'a quiet older-page retry scheduled for the old chat never fires into the new one')
    assert.deepEqual(refs.retry.current, { timer: 0, attempts: 0 },
      'the new chat starts its older-page backoff fresh')
    hook.unmount()
  } finally {
    globalThis.cancelAnimationFrame = previousCancel
    globalThis.clearTimeout = previousClearTimeout
  }
})
