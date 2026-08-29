export function isPendingQuestionSendFailure(error) {
  return Number(error?.status) === 409
    && error?.code === 'pending_question_open'
}

export function isModelSelectionRequiredFailure(error) {
  return Number(error?.status) === 409
    && error?.code === 'model_selection_required'
}

export function sendFailureMessage(error, { online = true } = {}) {
  if (!online) {
    return error?.outboxRetained
      ? 'You’re offline. Your message is queued and will send when you reconnect.'
      : 'You’re offline. Your message is back in the composer—send it when you reconnect.'
  }
  if (error?.name === 'ChatTransportError') {
    if (error?.outboxRetained) {
      return 'Möbius couldn’t confirm the send. Your message is queued and will retry automatically.'
    }
    return 'Möbius couldn’t confirm the send. Your message is back in the composer—retrying won’t send it twice.'
  }
  if (error?.name === 'AbortError') {
    if (error?.outboxRetained) {
      return 'Möbius took too long to confirm the send. Your message is queued and will retry automatically.'
    }
    return 'Möbius took too long to confirm the send. Your message is back in the composer—retrying won’t send it twice.'
  }
  const status = Number(error?.status)
  if (status === 503 || status >= 500) {
    if (error?.outboxRetained) {
      return 'Möbius can’t save messages right now. Your message is queued and will retry automatically.'
    }
    return 'Möbius can’t save messages right now. Your message is back in the composer—try again in a moment.'
  }
  if (status === 429) {
    if (error?.outboxRetained) {
      return 'Möbius is receiving too many requests right now. Your message is queued and will retry automatically.'
    }
    return 'Möbius is receiving too many requests right now. Your message is back in the composer—wait a moment and try again.'
  }
  if (status === 401 || status === 403) {
    if (error?.outboxRetained) {
      return 'Möbius needs you to sign in again. Your message is queued for this owner and will resume afterward.'
    }
    return 'Möbius needs you to sign in again before sending. Your message is safe in the composer.'
  }
  if (isPendingQuestionSendFailure(error)) {
    return 'Answer the pending question above, or Stop the turn. Your message is safe in the composer.'
  }
  if (isModelSelectionRequiredFailure(error)) {
    return 'Choose a model before sending. Your message is safe in the composer.'
  }
  return 'Möbius couldn’t send the message. It’s back in the composer—try again.'
}
