/* Retry a helper's exact parked attempt without creating a separate chat run. */
export async function retryHelperRecovery(request, delegationId, runId) {
  const response = await request(`/delegations/${encodeURIComponent(delegationId)}/retry`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ run_token: runId }),
  })
  const body = await response.json()
  if (!response.ok) {
    const detail = body?.detail
    throw Object.assign(new Error(
      (typeof detail === 'string' ? detail : detail?.message)
      || `Could not retry this helper (${response.status}). Open its parent chat to continue.`,
    ), { status: response.status, code: 'helper_recovery_unavailable' })
  }
  const running = ['accepted', 'retrying', 'starting', 'running', 'resuming'].includes(body.status)
  if (body.retry_started || running) {
    // A replacement may already have finished or parked again before the
    // acknowledgement. Never leave the composer busy on that settled result.
    return { status: body.status, running }
  }
  if (body.status === 'paused' && body.physical_run_id !== runId) {
    throw Object.assign(new Error('Recovery state changed.'), { code: 'recovery_changed', status: 409 })
  }
  throw Object.assign(new Error(
    body.status === 'paused'
      ? 'This helper could not retry. Its pause is unchanged. Open its parent chat to continue.'
      : 'This helper is no longer waiting for a retry. Open its parent chat to continue.',
  ), { code: 'helper_recovery_unavailable', status: 409 })
}
