import { useEffect, useRef } from 'react'
import {
  BASE, getToken, getAuthHeaders, isEphemeralAuth,
  clearExpiredOwnerSession,
} from '../api/client.js'
import * as setupSession from '../lib/setupSession.js'
import {
  reportNetworkReachable,
  verifyConnectivity,
} from '../lib/connectivityStore.js'

// Startup includes the bounded durable-state reconciliation before reads begin.
export const SYSTEM_CONNECT_DEADLINE_MS = 30_000
export const SYSTEM_READ_DEADLINE_MS = 70_000
export const SYSTEM_QUICK_WAKE_MS = 10_000

/**
 * Persistent SSE subscription to /api/events/system. Lives on the
 * Shell so system events (theme_updated, app_updated,
 * shell_rebuild_*) reach the listener even when the user is on the
 * canvas / settings / a different chat than the one whose agent
 * emitted the event.
 *
 * Why a separate stream from useStreamConnection: that hook is
 * scoped to a single chat's broadcast. Per-chat broadcasts close
 * 30s after the agent finishes, and lifecycle events can fire after
 * the chat is already done — leaving nowhere for the event to land. The
 * shell-level stream stays open for the lifetime of the Shell.
 *
 * EventSource isn't used because it can't send custom Authorization
 * headers; we use fetch + ReadableStream, mirroring the pattern in
 * useStreamConnection.
 *
 * The same event types are still forwarded via chat broadcasts for
 * in-chat catch-up coherence. Handlers should be idempotent (theme
 * reload, refreshApps, version bump) so duplicates are harmless.
 */
export default function useSystemEventStream(
  onEvent,
  { enabled = true, onOpen = null } = {},
) {
  // Mirror onEvent in a ref so the long-lived effect can call the
  // latest handler without re-running the connection setup whenever
  // the callback identity changes.
  const onEventRef = useRef(onEvent)
  useEffect(() => { onEventRef.current = onEvent }, [onEvent])
  const onOpenRef = useRef(onOpen)
  useEffect(() => { onOpenRef.current = onOpen }, [onOpen])

  useEffect(() => {
    if (!enabled) return undefined
    let stopped = false
    let active = null
    let retryTimer = null
    let backoffMs = 1000
    let hiddenAt = document.visibilityState === 'hidden' ? Date.now() : null

    const visible = () => document.visibilityState !== 'hidden'
    const owns = attempt => !stopped && active === attempt

    function clearRetry() {
      clearTimeout(retryTimer)
      retryTimer = null
    }

    function retire() {
      const attempt = active
      active = null
      if (!attempt) return
      clearTimeout(attempt.deadline)
      attempt.controller.abort()
    }

    function scheduleRetry() {
      if (stopped || !visible() || retryTimer !== null) return
      retryTimer = setTimeout(() => {
        retryTimer = null
        void connect()
      }, backoffMs)
      backoffMs = Math.min(backoffMs * 2, 30_000)
    }

    // The server emits bytes at least every 30s, including keepalive comments.
    // A deadline belongs to this connection, not to agent work: it never stops
    // a chat or resubmits an answer. Hidden documents defer it until return.
    function armDeadline(attempt) {
      clearTimeout(attempt.deadline)
      if (!visible()) return
      const deadlineAt = attempt.lastReadAt === null
        ? attempt.startedAt + SYSTEM_CONNECT_DEADLINE_MS
        : attempt.lastReadAt + SYSTEM_READ_DEADLINE_MS
      attempt.deadline = setTimeout(() => {
        if (!owns(attempt)) return
        retire()
        void verifyConnectivity()
        scheduleRetry()
      }, Math.max(0, deadlineAt - Date.now()))
    }

    async function reconcile(attempt) {
      const { signal } = attempt.controller
      let onAbort
      const aborted = new Promise((_, reject) => {
        onAbort = () => reject(signal.reason)
        signal.addEventListener('abort', onAbort, { once: true })
      })
      try {
        await Promise.race([onOpenRef.current?.({ signal }), aborted])
      } finally {
        signal.removeEventListener('abort', onAbort)
      }
    }

    async function connect() {
      if (stopped || active) return
      clearRetry()
      if (!getToken()) {
        if (isEphemeralAuth()) scheduleRetry()
        return
      }
      const attempt = {
        controller: new AbortController(),
        startedAt: Date.now(),
        lastReadAt: null,
        deadline: null,
      }
      active = attempt
      armDeadline(attempt)
      let reader
      try {
        const res = await fetch(`${BASE}/api/events/system`, {
          headers: getAuthHeaders(),
          signal: attempt.controller.signal,
        })
        if (!owns(attempt)) return
        reportNetworkReachable()
        if (res.status === 401) {
          if (isEphemeralAuth()) {
            window.dispatchEvent(new CustomEvent('mobius:chat-embed-auth-expired'))
            throw new Error('EMBED_AUTH_EXPIRED')
          }
          // Renewal preserves principal-partitioned accepted owner intent.
          // Never turn an expired credential into an endless reconnect loop.
          stopped = true
          if (!setupSession.isInProgress()) {
            await clearExpiredOwnerSession()
            setTimeout(() => window.location.reload(), 100)
          }
          return
        }
        if (!res.ok || !res.body) throw new Error(`system stream status ${res.status}`)
        // Durable chat truth must settle before buffered events are applied.
        // The connection deadline also bounds this barrier; cancellation is
        // passed to its reads so an old attempt cannot publish a late snapshot.
        await reconcile(attempt)
        if (!owns(attempt)) return

        reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        while (owns(attempt)) {
          const { value, done } = await reader.read()
          if (!owns(attempt) || done) break
          attempt.lastReadAt = Date.now()
          backoffMs = 1000
          armDeadline(attempt)
          buffer += decoder.decode(value, { stream: true })
          let nl
          while ((nl = buffer.indexOf('\n\n')) !== -1) {
            const block = buffer.slice(0, nl)
            buffer = buffer.slice(nl + 2)
            for (const line of block.split('\n')) {
              if (!line.startsWith('data: ')) continue
              try {
                const ev = JSON.parse(line.slice(6))
                if (ev && ev.type && ev.type !== 'system_stream_open') {
                  onEventRef.current?.(ev)
                }
              } catch { /* malformed — skip */ }
            }
          }
        }
      } catch {
        // Only the current attempt may schedule recovery. Aborted predecessors
        // can finish after a replacement has already connected.
      } finally {
        reader?.releaseLock()
        if (active === attempt) {
          retire()
          if (!stopped) {
            void verifyConnectivity()
            scheduleRetry()
          }
        }
      }
    }

    function onWake() {
      if (!visible() || stopped) return
      const now = Date.now()
      const longAbsence = hiddenAt !== null && now - hiddenAt >= SYSTEM_QUICK_WAKE_MS
      hiddenAt = null
      // Preserve healthy quick switches and an already-started reconnect. A
      // burst of visibility/focus/pageshow/online events still owns one socket.
      if (active && !longAbsence) {
        const fresh = active.lastReadAt === null
          ? now - active.startedAt < SYSTEM_CONNECT_DEADLINE_MS
          : now - active.lastReadAt < SYSTEM_READ_DEADLINE_MS
        if (fresh) {
          armDeadline(active)
          return
        }
      }
      clearRetry()
      retire()
      void connect()
    }

    function onVisibility() {
      if (!visible()) {
        hiddenAt = Date.now()
        clearRetry()
        if (active) clearTimeout(active.deadline)
      } else {
        onWake()
      }
    }

    document.addEventListener('visibilitychange', onVisibility)
    window.addEventListener('focus', onWake)
    window.addEventListener('pageshow', onWake)
    window.addEventListener('online', onWake)
    void connect()

    return () => {
      stopped = true
      clearRetry()
      retire()
      document.removeEventListener('visibilitychange', onVisibility)
      window.removeEventListener('focus', onWake)
      window.removeEventListener('pageshow', onWake)
      window.removeEventListener('online', onWake)
    }
  }, [enabled])
}
