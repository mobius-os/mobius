export const AUTO_RESUME_REQUEST_TIMEOUT_MS = 15000
export const MAX_TIMER_DELAY_MS = 2_147_483_647


function policyValue(data, field) {
  return !!data?.[field]
}


async function responseDetail(res) {
  try {
    return (await res.json()).detail || ''
  } catch {
    return ''
  }
}


function mutationErrorMessage(err) {
  if (err?.name === 'AbortError') {
    return 'Could not save this chat setting before the request timed out.'
  }
  return err?.message || 'Could not save this chat setting.'
}


/**
 * Persist the chat-local auto-resume policy and converge on server truth when
 * the mutation result is ambiguous. A PATCH can commit successfully while its
 * response is lost; treating that as a definite failure would leave an OFF
 * switch controlling an ON server policy.
 */
async function saveBooleanPolicy({ chatId, next, request, field }) {
  const desired = !!next
  let mutationError = null

  try {
    const res = await request(`/chats/${encodeURIComponent(chatId)}`, {
      method: 'PATCH',
      body: JSON.stringify({ [field]: desired }),
      timeoutMs: AUTO_RESUME_REQUEST_TIMEOUT_MS,
    })
    if (!res.ok) {
      const detail = await responseDetail(res)
      throw new Error(detail || 'Could not save this chat setting.')
    }
    const data = await res.json()
    return { value: policyValue(data, field), error: '' }
  } catch (err) {
    mutationError = err
  }

  try {
    const res = await request(
      `/chats/${encodeURIComponent(chatId)}?limit=1`,
      { timeoutMs: AUTO_RESUME_REQUEST_TIMEOUT_MS },
    )
    if (!res.ok) throw new Error(`policy reconciliation failed (${res.status})`)
    const value = policyValue(await res.json(), field)
    return {
      value,
      // A lost response after a successful commit is a success once GET proves
      // the desired value. If the server retained the old value, preserve the
      // original mutation error so the owner knows to retry.
      error: value === desired
        ? ''
        : mutationErrorMessage(mutationError),
    }
  } catch {
    return {
      value: null,
      error: `${mutationErrorMessage(mutationError)} Current setting could not be verified.`,
    }
  }
}


export function saveAutoResumePolicy(args) {
  return saveBooleanPolicy({...args, field: 'auto_resume_on_limit'})
}


export function saveRestartResumePolicy(args) {
  return saveBooleanPolicy({...args, field: 'auto_resume_on_restart'})
}


export function resetDeadlineState(resetAt, now = Date.now()) {
  if (!resetAt) return { elapsed: false, remainingMs: null }
  const remainingMs = Date.parse(resetAt) - now
  if (!Number.isFinite(remainingMs)) {
    return { elapsed: true, remainingMs: null }
  }
  return {
    elapsed: remainingMs <= 0,
    remainingMs,
  }
}


/**
 * Return the next safe wake-up. Long deadlines are revisited at the browser's
 * maximum timer delay instead of being incorrectly marked elapsed then.
 */
export function resetDeadlineDelay(resetAt, now = Date.now()) {
  const state = resetDeadlineState(resetAt, now)
  if (state.elapsed || state.remainingMs === null) return null
  return Math.min(state.remainingMs + 250, MAX_TIMER_DELAY_MS)
}
