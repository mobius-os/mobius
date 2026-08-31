export function isPendingQuestionSendFailure(error) {
  return Number(error?.status) === 409
    && error?.code === 'pending_question_open'
}

export function isModelSelectionRequiredFailure(error) {
  return Number(error?.status) === 409
    && error?.code === 'model_selection_required'
}

export function isAmbiguousSendFailure(error) {
  return error?.name === 'ChatTransportError'
    || error?.name === 'AbortError'
}

export function sendFailureMessage(error, { online = true } = {}) {
  if (!online) {
    return 'You’re offline. Your message is back in the composer—send it when you reconnect.'
  }
  if (isAmbiguousSendFailure(error)) {
    return 'Checking whether that message reached the chat… It’s safe here while Möbius confirms.'
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
  if (isPendingQuestionSendFailure(error)) {
    return 'Answer the pending question above, or Stop the turn. Your message is safe in the composer.'
  }
  if (isModelSelectionRequiredFailure(error)) {
    return 'Choose a model before sending. Your message is safe in the composer.'
  }
  return 'Möbius couldn’t send the message. It’s back in the composer—try again.'
}
