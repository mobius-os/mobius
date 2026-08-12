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
