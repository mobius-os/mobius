export class ChatTransportError extends Error {
  constructor(cause) {
    super('The chat request did not reach Möbius', { cause })
    this.name = 'ChatTransportError'
  }
}

export class ChatHttpError extends Error {
  constructor(status, { code = null, detail = null } = {}) {
    super(`HTTP ${status}`)
    this.name = 'ChatHttpError'
    this.status = Number(status)
    this.code = typeof code === 'string' ? code : null
    this.detail = typeof detail === 'string' ? detail : null
  }
}

export async function chatHttpError(response) {
  let code = null
  let detail = null
  try {
    const payload = await response.json()
    const responseDetail = payload?.detail
    code = typeof responseDetail?.code === 'string'
      ? responseDetail.code
      : null
    detail = typeof responseDetail === 'string'
      ? responseDetail
      : typeof responseDetail?.message === 'string'
        ? responseDetail.message
        : null
  } catch {}
  return new ChatHttpError(response.status, { code, detail })
}

export function isQuestionStateChangedError(error) {
  if (Number(error?.status) === 410) return true
  if (error?.code === 'question_state_changed') return true
  // Rolling-update compatibility: the older server used exactly these two
  // generic 409 messages for settled cards. Keep current and future validation
  // failures retryable rather than inferring lifecycle from a shared prefix.
  return Number(error?.status) === 409
    && typeof error?.detail === 'string'
    && (
      error.detail === 'This Restart card is stale or no longer accepting a response.'
      || error.detail === 'This Restart card is stale or no longer authorized.'
    )
}
