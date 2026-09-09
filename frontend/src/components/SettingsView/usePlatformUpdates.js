/** Owns update requests and reconnects; durable rebuild state stays with its controller. */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { api } from '../../api/client.js'
import { versionQueries } from '../../hooks/queries.js'
import { rebuildIsActive, rebuildRequestOutcome } from '../../lib/containerRebuild.js'
import { platformStatusFromApply, platformStatusUnavailable } from '../../lib/platformUpdateState.js'
import { restartCanReload, restartPollDecision } from '../../lib/restartReadiness.js'
import { inspectShellUpdate, releaseWaitingShellUpdate } from '../../lib/shellUpdate.js'

async function responseBody(response) {
  const body = await response.json().catch(() => null)
  if (!response.ok) {
    const detail = body?.detail
    const error = new Error(body?.error || detail?.message || (typeof detail === 'string' && detail)
      || body?.message || 'Möbius could not complete this request. Try again.')
    error.code = detail?.code || ''
    error.definitive = response.status >= 400 && response.status < 500
    throw error
  }
  return body
}

async function health() {
  try {
    const response = await fetch('/api/health', { cache: 'no-store', credentials: 'same-origin' })
    const body = await response.json()
    return { ok: response.ok, bootId: body?.boot_id || '' }
  } catch { return { ok: false, bootId: '' } }
}

async function reloadShell() {
  try {
    const response = await fetch('/shell/', {
      cache: 'no-store', credentials: 'same-origin', headers: { Accept: 'text/html' },
    })
    if (!response.ok || !response.headers.get('content-type')?.includes('text/html')) return false
    try {
      const { registration } = await inspectShellUpdate({ serviceWorker: navigator.serviceWorker })
      releaseWaitingShellUpdate(registration)
    } catch { /* Online document navigation owns freshness, not the offline cache. */ }
    try { sessionStorage.setItem('mobius:return-view', 'settings') } catch {}
    window.location.reload()
    return true
  } catch { return false }
}

export default function usePlatformUpdates({ active, refreshToken, onOpenChat }) {
  const queryClient = useQueryClient()
  const versionQuery = versionQueries.current.useQuery()
  const [platform, setPlatform] = useState(null)
  const [rebuild, setRebuild] = useState(null)
  const [phase, setPhase] = useState('idle')
  const [error, setError] = useState('')
  const [errorCode, setErrorCode] = useState('')
  const [checkResult, setCheckResult] = useState('')
  const [progress, setProgress] = useState(null)
  const [reconnect, setReconnect] = useState(null)
  const [slow, setSlow] = useState(false)
  const pending = useRef(false)
  const busy = phase !== 'idle' || rebuildIsActive(rebuild) || !!reconnect

  const refreshPlatform = useCallback(async ({ preserveCurrentOnFailure = false } = {}) => {
    try {
      const body = await responseBody(await api.platform.status())
      if (!body || typeof body.state !== 'string') throw new Error('Unreadable update status')
      setPlatform(body)
      return body
    } catch {
      if (!preserveCurrentOnFailure) setPlatform(current => platformStatusUnavailable(current))
      return null
    }
  }, [])

  const refreshRebuild = useCallback(async () => {
    try {
      const body = await responseBody(await api.admin.rebuildStatus())
      if (!body || typeof body.state !== 'string') throw new Error('Unreadable container status')
      setRebuild(body)
      return body
    } catch {
      // Keep the durable job visible during the expected cutover disconnect.
      setRebuild(current => ({ ...current, status_unavailable: true }))
      return null
    }
  }, [])

  useEffect(() => {
    if (!active) return
    refreshPlatform()
    refreshRebuild()
  }, [active, refreshToken, refreshPlatform, refreshRebuild])

  useEffect(() => {
    if (!active || !rebuildIsActive({ state: rebuild?.state }) || reconnect) return
    let cancelled = false
    let timer
    const poll = async () => {
      const body = await refreshRebuild()
      if (cancelled) return
      if (!body || rebuildIsActive(body)) timer = window.setTimeout(poll, 1500)
      else refreshPlatform()
    }
    timer = window.setTimeout(poll, 1500)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [active, rebuild?.state, reconnect, refreshPlatform, refreshRebuild])

  // This single observation loop also owns ambiguous responses. It only reads:
  // a lost response must never turn into an automatic second mutation.
  useEffect(() => {
    if (!reconnect) return
    let cancelled = false
    let timer
    let attempts = 0
    let sawUnavailable = false
    let freshServerSeen = false
    const started = Date.now()
    function settled() { setReconnect(null); setPhase('idle'); refreshPlatform() }
    const poll = async () => {
      attempts += 1
      if (reconnect.kind === 'apply') {
        try {
          const result = await responseBody(await api.platform.updateProgress())
          if (cancelled) return
          setProgress(result)
          if (result?.plan_id === reconnect.plan.plan_id && !result.active) {
            setError(result.error || (result.phase === 'blocked' ? 'The update needs attention. Review its current status.' : ''))
            settled()
            return
          }
        } catch { /* The exact operation may still be running. Keep observing. */ }
      } else {
        if (reconnect.kind === 'rebuild') {
          const result = await refreshRebuild()
          if (cancelled) return
          const currentOperation = result && result.expected_sha === reconnect.plan.target_sha && (
            (result.operation_id && result.operation_id !== reconnect.previousOperationId)
            || Date.parse(result.updated_at || '') >= reconnect.requestedAt
          )
          if (currentOperation && ['failed', 'rolled_back', 'needs_recovery', 'no_change'].includes(result.state)) {
            setError(result.error || result.message || '')
            settled()
            return
          }
        }
        const current = await health()
        if (cancelled) return
        if (!current.ok) sawUnavailable = true
        else if (restartCanReload({
          previousBootId: reconnect.bootId, currentBootId: current.bootId,
          sawUnavailable, elapsedMs: Date.now() - started,
        })) {
          freshServerSeen = true
          if (await reloadShell()) return
        }
      }
      if (cancelled) return
      const next = restartPollDecision(attempts)
      setSlow(next.slow)
      if (next.timedOut) {
        settled()
        setError(freshServerSeen
          ? 'Möbius restarted, but the page could not refresh. Refresh this page.'
          : 'The request could not be confirmed. Check the current status and review again before retrying.')
        setErrorCode('update_plan_stale')
      } else timer = window.setTimeout(poll, next.delayMs)
    }
    timer = window.setTimeout(poll, restartPollDecision(0).delayMs)
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [reconnect, refreshPlatform, refreshRebuild])

  useEffect(() => {
    if (phase !== 'applying') return
    let cancelled = false
    let timer
    const poll = async () => {
      try {
        const body = await responseBody(await api.platform.updateProgress())
        if (!cancelled) setProgress(body)
      } catch { /* Explanatory progress never replaces the mutation's outcome. */ }
      if (!cancelled) timer = window.setTimeout(poll, 500)
    }
    poll()
    return () => { cancelled = true; window.clearTimeout(timer) }
  }, [phase])

  async function check() {
    if (pending.current || busy) return
    pending.current = true
    setPhase('checking'); setError(''); setCheckResult('')
    const results = await Promise.allSettled([
      (async () => {
        const body = await responseBody(await api.platform.check())
        setPlatform(body)
        return body
      })(),
      (async () => {
        await inspectShellUpdate({ serviceWorker: navigator.serviceWorker })
        await versionQueries.current.invalidate(queryClient)
        const result = await versionQuery.refetch()
        if (result.isError) throw result.error
      })(),
    ])
    if (results[0].status === 'rejected') setPlatform(current => platformStatusUnavailable(current))
    const failure = results.find(result => result.status === 'rejected')
    setCheckResult(failure ? 'Couldn’t finish checking. Try again.' : 'Checked just now')
    await refreshRebuild()
    pending.current = false
    setPhase('idle')
    return results[0].status === 'fulfilled' ? results[0].value : null
  }

  async function execute(plan, kind) {
    if (pending.current || busy) return { ok: false }
    pending.current = true
    setPhase(kind === 'rebuild' ? 'rebuilding' : 'applying')
    setError(''); setErrorCode(''); setProgress(null); setCheckResult('')
    const requestedAt = Date.now()
    const previousOperationId = rebuild?.operation_id
    const before = kind === 'rebuild' ? await health() : null
    try {
      const body = await responseBody(await (kind === 'rebuild'
        ? api.platform.rebuild(plan) : api.platform.apply(plan)))
      if (kind === 'rebuild' && body?.state !== 'conflict' && (body?.state !== 'rolled_back' || body?.supported !== undefined)) {
        setRebuild(body)
        const outcome = rebuildRequestOutcome(body, { reviewedUpdate: true })
        if (outcome.cutoverAccepted) setReconnect({ kind: 'rebuild', bootId: before.bootId, plan, requestedAt, previousOperationId })
        if (outcome.alreadyCurrent) await refreshPlatform()
        if (!outcome.accepted) setError(body?.error || body?.message || 'The update could not finish.')
        return { ok: outcome.accepted, state: body?.state }
      }
      const state = body?.state
      if (['restart_needed', 'activation_needed', 'up_to_date', 'conflict', 'rolled_back'].includes(state)) {
        setPlatform(current => platformStatusFromApply(current, body))
        await refreshPlatform({ preserveCurrentOnFailure: true })
        if (state === 'conflict' || state === 'rolled_back') {
          if (body?.error) setError(body.error)
          return { ok: false, state }
        }
        return { ok: true, state }
      }
      setError('The update returned an unexpected result. Check its status before trying again.')
      return { ok: false, state }
    } catch (cause) {
      setError(cause.message || 'The update could not be confirmed.')
      setErrorCode(cause.code || '')
      if (!cause.definitive) {
        setReconnect({ kind, plan, bootId: before?.bootId || '', requestedAt, previousOperationId })
        setError('The connection was interrupted. Checking whether your update started; no second request will be sent.')
      } else await Promise.all([
        refreshPlatform({ preserveCurrentOnFailure: true }),
        refreshRebuild(),
      ])
      return { ok: false }
    } finally {
      pending.current = false
      setPhase('idle')
    }
  }

  async function restart() {
    if (pending.current || busy) return
    pending.current = true
    setPhase('restarting'); setError(''); setErrorCode(''); setSlow(false)
    const before = await health()
    try {
      await responseBody(await api.admin.restart())
      setReconnect({ kind: 'restart', bootId: before.bootId })
    } catch (cause) {
      setError(cause.message || 'The restart could not be confirmed. Check its status before trying again.')
      if (!cause.definitive) setReconnect({ kind: 'restart', bootId: before.bootId })
      else setPhase('idle')
    } finally { pending.current = false }
  }

  async function resolve() {
    if (pending.current || busy || !onOpenChat) return
    if (platform?.conflict_chat_id) { onOpenChat(platform.conflict_chat_id); return }
    pending.current = true
    setPhase('resolving'); setError('')
    try {
      const body = await responseBody(await api.platform.conflictResolverChat())
      await refreshPlatform()
      if (body?.chat_id) onOpenChat(body.chat_id)
    } catch (cause) { setError(cause.message || 'Could not open the repair chat.') }
    finally { pending.current = false; setPhase('idle') }
  }

  const clearError = useCallback(() => { setError(''); setErrorCode('') }, [])

  return {
    platform, rebuild, version: versionQuery.data, phase, busy, error, errorCode, checkResult,
    progress, reconnecting: !!reconnect, observingKind: reconnect?.kind, slow, check, execute, restart, resolve,
    clearError,
  }
}
