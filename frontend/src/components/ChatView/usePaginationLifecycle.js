import { useLayoutEffect, useRef } from 'react'


/**
 * Fence asynchronous history work at the commit boundary that replaces its
 * transcript. Passive activation cleanup is too late: a resolved promise can
 * run after the next chat commits but before React flushes passive effects.
 */
export default function usePaginationLifecycle({
  chatId,
  hidden,
  loadNonce,
  provisionalNewChat,
  searchAnchorKey,
  searchRevealId,
  loadingOlderRef,
  followupRafRef,
}) {
  const lifecycleRef = useRef(0)

  useLayoutEffect(() => {
    lifecycleRef.current += 1
    return () => {
      // Layout cleanup runs inside the commit that retires this activation, so
      // an old response cannot enter the new transcript before passive cleanup.
      lifecycleRef.current += 1
      const followupRaf = followupRafRef.current
      if (followupRaf) cancelAnimationFrame(followupRaf)
      followupRafRef.current = 0
      loadingOlderRef.current = false
    }
  }, [
    chatId,
    hidden,
    loadNonce,
    provisionalNewChat,
    searchAnchorKey,
    searchRevealId,
    loadingOlderRef,
    followupRafRef,
  ])

  return lifecycleRef
}
