import { useLayoutEffect, useRef } from 'react'

/** Retire either a fetched page or its delayed paint/quiet retry atomically. */
export function cancelOlderPageWork({ pageRef, loadingOlderRef, followupRafRef, retryRef }) {
  const page = pageRef.current
  if (page) {
    page.cancelled = true
    if (page.frame) cancelAnimationFrame(page.frame)
  }
  pageRef.current = null
  if (followupRafRef.current) cancelAnimationFrame(followupRafRef.current)
  followupRafRef.current = 0
  if (retryRef.current.timer) clearTimeout(retryRef.current.timer)
  retryRef.current.timer = 0
  loadingOlderRef.current = false
}

export function olderPageIsCurrent({ page, pageRef, lifecycle, lifecycleRef, chatStale }) {
  return lifecycleRef.current === lifecycle
    && pageRef.current === page
    && !page.cancelled
    && !chatStale
}


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
  pageRef,
  followupRafRef,
  retryRef,
}) {
  const lifecycleRef = useRef(0)

  useLayoutEffect(() => {
    lifecycleRef.current += 1
    return () => {
      // Layout cleanup runs inside the commit that retires this activation, so
      // an old response cannot enter the new transcript before passive cleanup.
      lifecycleRef.current += 1
      cancelOlderPageWork({ pageRef, loadingOlderRef, followupRafRef, retryRef })
      retryRef.current = { timer: 0, attempts: 0 }
    }
  }, [
    chatId,
    hidden,
    loadNonce,
    provisionalNewChat,
    searchAnchorKey,
    searchRevealId,
    loadingOlderRef,
    pageRef,
    followupRafRef,
    retryRef,
  ])

  return lifecycleRef
}
