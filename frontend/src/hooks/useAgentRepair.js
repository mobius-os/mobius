import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  errorRecoveryFingerprint,
  readErrorRecoveryAttempt,
  runAgentRepair,
  writeRefreshedRecoveryAttempt,
} from '../lib/errorRecovery.js'

/**
 * "Ask the agent to fix it" for one failure of one surface: owns that
 * failure's entry in the shared recovery ledger, runs at most one repair at a
 * time, and navigates to the repair chat on success.
 *
 * `fingerprint` names the failure. A crash surface passes the one it derived
 * from the error (null while nothing has failed: repair is then a no-op); a
 * surface with no crash to fingerprint (a degraded fallback) omits it and is
 * keyed by the surface alone. A new fingerprint reads its own
 * ledger entry. A bfcache restore re-reads it and drops any repair still in
 * flight: the navigation that repair was waiting on has been undone.
 * `markRefreshed` records a manual refresh so a still-broken reload escalates
 * to the agent.
 */
export default function useAgentRepair({
  surfaceKey,
  fingerprint = errorRecoveryFingerprint(surfaceKey, surfaceKey, ''),
  prompt,
  repairTransport,
}) {
  const stored = useMemo(
    () => (fingerprint ? readErrorRecoveryAttempt({ surfaceKey, fingerprint }) : null),
    [fingerprint, surfaceKey],
  )
  // What the ledger holds for this failure since that read: the attempt a
  // running repair persisted, or the re-read after a bfcache restore.
  const [live, setLive] = useState(null)
  const attempt = live?.fingerprint === fingerprint ? live.attempt : stored
  const [repairActive, setRepairActive] = useState(false)
  const [startupError, setStartupError] = useState(null)
  const controllerRef = useRef(null)

  const cancel = useCallback(() => {
    const controller = controllerRef.current
    controllerRef.current = null
    controller?.abort()
  }, [])

  useEffect(() => cancel, [cancel])

  useEffect(() => {
    if (!fingerprint) return undefined
    const onPageShow = (event) => {
      if (!event.persisted) return
      cancel()
      setLive({
        fingerprint, attempt: readErrorRecoveryAttempt({ surfaceKey, fingerprint }),
      })
      setRepairActive(false)
    }
    window.addEventListener('pageshow', onPageShow)
    return () => window.removeEventListener('pageshow', onPageShow)
  }, [cancel, fingerprint, surfaceKey])

  const repair = useCallback(async () => {
    if (!fingerprint || controllerRef.current) return
    const controller = new AbortController()
    controllerRef.current = controller
    setRepairActive(true)
    setStartupError(null)
    try {
      let client
      let base
      if (repairTransport) {
        ({ client, base } = repairTransport())
      } else {
        const module = await import('../api/client.js')
        client = module.api
        base = module.BASE
      }
      const result = await runAgentRepair({
        client,
        base,
        surfaceKey,
        fingerprint,
        previousAttempt: attempt,
        signal: controller.signal,
        onAttempt: next => {
          if (controllerRef.current === controller) setLive({ fingerprint, attempt: next })
        },
        prompt,
      })
      if (controllerRef.current === controller && !controller.signal.aborted) {
        window.location.assign(result.path)
      }
    } catch (error) {
      // Loading the client can fail before the shared runner records an attempt.
      if (controllerRef.current === controller && !controller.signal.aborted && error?.name !== 'AbortError') {
        setStartupError({ fingerprint, message: 'Couldn’t open the chat. Try again.' })
      }
    } finally {
      if (controllerRef.current === controller) {
        controllerRef.current = null
        setRepairActive(false)
      }
    }
  }, [attempt, fingerprint, prompt, repairTransport, surfaceKey])

  const markRefreshed = useCallback(() => {
    if (fingerprint) writeRefreshedRecoveryAttempt({ surfaceKey, fingerprint })
  }, [fingerprint, surfaceKey])

  return { attempt, repairActive, repair, markRefreshed, error: startupError?.fingerprint === fingerprint ? startupError.message : '' }
}
