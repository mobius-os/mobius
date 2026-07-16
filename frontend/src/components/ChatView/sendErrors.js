export class ChatTransportError extends Error {
  constructor(cause) {
    super('The chat request did not reach Möbius', { cause })
    this.name = 'ChatTransportError'
  }
}

export class ChatHttpError extends Error {
  constructor(status, detail = null) {
    super(`HTTP ${status}`)
    this.name = 'ChatHttpError'
    this.status = Number(status)
    this.detail = typeof detail === 'string' ? detail : null
  }
}

export async function chatHttpError(response) {
  let detail = null
  try {
    const payload = await response.json()
    detail = typeof payload?.detail === 'string'
      ? payload.detail
      : typeof payload?.detail?.message === 'string'
        ? payload.detail.message
        : null
  } catch {}
  return new ChatHttpError(response.status, detail)
}
