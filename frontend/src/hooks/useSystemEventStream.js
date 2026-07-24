import { useEffect, useRef } from 'react'
import {
  BASE, getToken, getAuthHeaders, isEphemeralAuth,
  clearToken, clearQueryCache,
} from '../api/client.js'
import * as setupSession from '../lib/setupSession.js'

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
    let cancelled = false
    let controller = null
    let backoffMs = 1000

    async function connect() {
      if (cancelled) return
      const token = getToken()
      if (!token) {
        // No token — nothing to authenticate with. Retry once the
        // user logs in (the effect re-runs because Shell remounts
        // on the auth boundary).
        if (isEphemeralAuth()) setTimeout(connect, 1000)
        return
      }
      controller = new AbortController()
      try {
        const res = await fetch(`${BASE}/api/events/system`, {
          headers: getAuthHeaders(),
          signal: controller.signal,
        })
        if (res.status === 401) {
          if (isEphemeralAuth()) {
            window.dispatchEvent(new CustomEvent('mobius:chat-embed-auth-expired'))
            throw new Error('EMBED_AUTH_EXPIRED')
          }
          // Stale / expired token. Reconnecting with the same token would
          // loop forever. Mirror apiFetch's AUTH_EXPIRED path: clear local
          // credentials and reload so the auth boundary takes over.
          // (Guard against the setup-wizard flow where 401s are expected.)
          if (!setupSession.isInProgress()) {
            clearToken()
            try { sessionStorage.setItem('auth_expired', '1') } catch {}
            await clearQueryCache()
            setTimeout(() => window.location.reload(), 100)
          }
          // Stop the reconnect loop regardless — either the page is
          // reloading or the setup-wizard will re-authenticate.
          cancelled = true
          return
        }
        if (!res.ok || !res.body) {
          throw new Error(`system stream status ${res.status}`)
        }
        backoffMs = 1000  // Reset on a successful connect.
        onOpenRef.current?.()

        const reader = res.body.getReader()
        const decoder = new TextDecoder()
        let buffer = ''
        // Standard SSE parser: events are terminated by a blank line;
        // each event is one or more `data: ` prefixed lines (we only
        // emit single-line events from the backend).
        while (!cancelled) {
          const { value, done } = await reader.read()
          if (done) break
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
      } catch (err) {
        if (cancelled || err.name === 'AbortError') return
      } finally {
        controller = null
      }
      // Reconnect with capped exponential backoff. The shell-level
      // stream is supposed to live as long as the Shell — any drop
      // (network blip, server restart) should self-heal.
      if (!cancelled) {
        await new Promise(r => setTimeout(r, backoffMs))
        backoffMs = Math.min(backoffMs * 2, 30_000)
        connect()
      }
    }

    connect()

    return () => {
      cancelled = true
      controller?.abort()
    }
  }, [enabled])
}
