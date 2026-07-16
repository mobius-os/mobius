export function sendFailureMessage(error, { online = true } = {}) {
  if (!online || error?.name === 'ChatTransportError') {
    return 'Möbius couldn’t reach the server. Your message is back in the composer—check your connection and try again.'
  }
  if (error?.name === 'AbortError') {
    return 'Möbius took too long to respond. Your message is back in the composer—try again.'
  }
  const status = Number(error?.status)
  if (status === 503 || status >= 500) {
    return 'Möbius can’t save messages right now. Your message is back in the composer—try again in a moment.'
  }
  if (status === 429) {
    return 'Möbius is receiving too many requests right now. Your message is back in the composer—wait a moment and try again.'
  }
  if (status === 401 || status === 403) {
    return 'Möbius needs you to sign in again before sending. Your message is safe in the composer.'
  }
  return 'Möbius couldn’t send the message. It’s back in the composer—try again.'
}
