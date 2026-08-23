/** Pure ordering state for releasing a submitted question card back to follow. */

export function questionAnswerHandoffReducer(state, event) {
  switch (event?.type) {
    case 'submitted':
      if (!event.submission || !event.questionKey) return null
      return {
        submission: event.submission,
        questionKey: event.questionKey,
        accepted: false,
        responseActivity: false,
      }

    case 'accepted':
      if (!state || state.submission !== event.submission || state.accepted) {
        return state
      }
      return { ...state, accepted: true }

    case 'response_activity':
      if (!state || state.questionKey !== event.questionKey
          || state.responseActivity) {
        return state
      }
      return { ...state, responseActivity: true }

    case 'stream_ended':
      // A terminal flush can publish response activity immediately before the
      // end event. Preserve that ready half until the layout effect runs.
      return state?.responseActivity ? state : null

    case 'cancelled':
    case 'released':
      if (!state || (event.submission
          && state.submission !== event.submission)) {
        return state
      }
      return null

    default:
      return state
  }
}

export function questionAnswerHandoffReady(state) {
  return Boolean(state?.accepted && state.responseActivity)
}
