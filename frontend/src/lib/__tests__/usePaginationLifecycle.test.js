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
    followupRafRef: refs.followup,
  }
}


test('a chat-switch layout commit retires old pagination before passive cleanup', () => {
  const previousCancel = globalThis.cancelAnimationFrame
  const cancelled = []
  globalThis.cancelAnimationFrame = frame => cancelled.push(frame)
  try {
    const refs = {
      loading: { current: true },
      followup: { current: 41 },
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
    hook.rerender(args('new-chat', refs))

    assert.equal(oldResponseIsCurrent(), false,
      'the retired response cannot publish rows into the new transcript')
    assert.equal(refs.loading.current, false)
    assert.equal(refs.followup.current, 0)
    assert.deepEqual(cancelled, [42])
    hook.unmount()
  } finally {
    globalThis.cancelAnimationFrame = previousCancel
  }
})
