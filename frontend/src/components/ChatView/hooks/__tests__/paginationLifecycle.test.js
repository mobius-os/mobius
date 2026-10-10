// Retired history work cannot paint into a newer reader action or chat.
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { renderHook } from './react-hook-shim.mjs'
import usePaginationLifecycle, {
  cancelOlderPageWork,
  olderPageIsCurrent,
} from '../../usePaginationLifecycle.js'

const ref = current => ({ current })

test('send or Q&A cancellation retires the active page, both frame lanes, and quiet retry', () => {
  const cancelled = []
  const prior = globalThis.cancelAnimationFrame
  globalThis.cancelAnimationFrame = id => cancelled.push(id)
  const timer = setTimeout(() => assert.fail('retired retry fired'), 1000)
  try {
    const page = { frame: 12, cancelled: false }
    const refs = {
      pageRef: ref(page), loadingOlderRef: ref(true),
      followupRafRef: ref(13), retryRef: ref({ timer, attempts: 2 }),
    }
    const lifecycleRef = ref(4)
    assert.equal(olderPageIsCurrent({ page, pageRef: refs.pageRef,
      lifecycle: 4, lifecycleRef, chatStale: false }), true)
    cancelOlderPageWork(refs)
    assert.deepEqual(cancelled, [12, 13])
    assert.equal(refs.pageRef.current, null)
    assert.equal(refs.loadingOlderRef.current, false)
    assert.equal(refs.followupRafRef.current, 0)
    assert.equal(refs.retryRef.current.timer, 0)
    assert.equal(olderPageIsCurrent({ page, pageRef: refs.pageRef,
      lifecycle: 4, lifecycleRef, chatStale: false }), false,
    'a late network response or frame cannot prepend after cancellation')
  } finally {
    clearTimeout(timer)
    globalThis.cancelAnimationFrame = prior
  }
})

test('a chat switch layout cleanup fences queued older-page paint before the next chat', () => {
  const cancelled = []
  const prior = globalThis.cancelAnimationFrame
  globalThis.cancelAnimationFrame = id => cancelled.push(id)
  const props = {
    chatId: 'a', hidden: false, loadNonce: 0, provisionalNewChat: false,
    searchAnchorKey: null, searchRevealId: null,
    loadingOlderRef: ref(true), pageRef: ref(null),
    followupRafRef: ref(0), retryRef: ref({ timer: 0, attempts: 2 }),
  }
  try {
    const h = renderHook(() => usePaginationLifecycle(props))
    const lifecycle = h.result.current.current
    const page = { frame: 22, cancelled: false }
    props.pageRef.current = page
    props.followupRafRef.current = 23
    props.chatId = 'b'
    h.rerender()
    assert.deepEqual(cancelled, [22, 23])
    assert.equal(page.cancelled, true)
    assert.equal(props.pageRef.current, null)
    assert.equal(props.loadingOlderRef.current, false)
    assert.equal(props.retryRef.current.attempts, 0)
    assert.equal(olderPageIsCurrent({ page, pageRef: props.pageRef,
      lifecycle, lifecycleRef: h.result.current, chatStale: false }), false)
    h.unmount()
  } finally {
    globalThis.cancelAnimationFrame = prior
  }
})
