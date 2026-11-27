/* What each entry of a request-list card is, and what a card shows of it. */

// A request-list card (block.input_requests) holds one entry per owner-input
// request. Questions and approvals are answered together by the card's shared
// Submit; a restart (Restart now) and a secret (sealed fields) each settle on
// their own, and what they settled is recorded in the block's
// `request_states`, keyed by request id. A missing `kind` is a question.

export const QUESTION = 'question'
export const APPROVAL = 'approval'
export const RESTART = 'restart'
export const SECRET = 'secret'


export function entryKind(entry) {
  return [APPROVAL, RESTART, SECRET].includes(entry?.kind) ? entry.kind : QUESTION
}


/**
 * The settled state of each entry: the server's record wins, and a state this
 * tab observed first (a response the block has not caught up with) fills in.
 */
export function mergedEntryStates(requestStates, localStates) {
  const server = requestStates && typeof requestStates === 'object' ? requestStates : {}
  const local = localStates && typeof localStates === 'object' ? localStates : {}
  return { ...local, ...server }
}


export function entryState(states, entry) {
  const state = entry?.id ? states?.[entry.id] : null
  return state && typeof state === 'object' && state.status ? state : null
}


/** The Restart now option the entry's platform action names, if it is well formed. */
export function restartEntryOption(entry) {
  const action = entry?.platform_action
  if (action?.type !== 'restart' || typeof action.restart_option_id !== 'string') return null
  return (entry.options || []).find(option => option?.id === action.restart_option_id) || null
}


/** The exact body fields of one Restart now press: nothing but this selection. */
export function restartPressSelection(entry) {
  const option = restartEntryOption(entry)
  return option && entry.id ? { [entry.id]: [option.id] } : null
}


/**
 * The answers the card's shared Submit (and its drafts) may carry. Choosing
 * Restart now on a restart entry is not an answer: only the entry's own
 * Continue sends it, as a press. A reply written on that entry is.
 */
export function sharedAnswerValues(questions, answers) {
  let shared = answers
  for (const entry of questions || []) {
    if (entryKind(entry) !== RESTART || !(entry.question in (answers || {}))) continue
    if (answers[entry.question] === '__other__') continue
    if (shared === answers) shared = { ...answers }
    delete shared[entry.question]
  }
  return shared
}


function answerGiven(value) {
  return Array.isArray(value) ? value.length > 0 : Boolean(value)
}


/**
 * The entries a card still shows. An open card shows every entry. Once the
 * card is no longer answerable it keeps only what was answered or settled on
 * it: the rest were never answered here and come back on the next card.
 */
export function shownEntries(questions, { answers = {}, states = {}, closed = false } = {}) {
  if (!closed) return questions
  return questions.filter(entry => (
    entryState(states, entry) || answerGiven(answers[entry.question])
  ))
}


/** Apply one `input_request_settled` to the matching card in a list of blocks. */
export function applyRequestEntryState(blocks, { question_id: questionId, request_id: requestId, state }) {
  if (!Array.isArray(blocks) || !questionId || typeof requestId !== 'string'
      || !state || typeof state !== 'object') return blocks
  let changed = false
  const next = blocks.map(block => {
    if (block?.type !== 'question' || block.question_id !== questionId
        || block.input_requests !== true
        || !(block.questions || []).some(entry => entry?.id === requestId)) return block
    const current = block.request_states?.[requestId]
    if (current && current.status === state.status && current.outcome === state.outcome) return block
    changed = true
    return { ...block, request_states: { ...(block.request_states || {}), [requestId]: state } }
  })
  return changed ? next : blocks
}


/** The same, for every assistant message of a transcript. */
export function applyRequestEntryStateToMessages(messages, event) {
  if (!Array.isArray(messages)) return messages
  let changed = false
  const next = messages.map(message => {
    if (message?.role !== 'assistant' || !Array.isArray(message.blocks)) return message
    const blocks = applyRequestEntryState(message.blocks, event)
    if (blocks === message.blocks) return message
    changed = true
    return { ...message, blocks }
  })
  return changed ? next : messages
}


/**
 * The chat's two card markers after the stream shows a live card. A
 * request-list card becomes the open list card; any other card bars the
 * chat, and saving it retired the open list card, so that card stops being
 * answerable at once instead of at the next reconcile.
 */
export function cardMarkersAfterLiveCard(questionId, { inputRequests = false } = {}) {
  return inputRequests
    ? { openRequestCardId: questionId }
    : { liveQuestionId: questionId, openRequestCardId: null }
}


/**
 * What the server's transcript says about one entry after a press whose
 * outcome is unknown: settled (with the state it recorded), still offered on
 * the open card, gone from it, or unknown when the card is not in view.
 */
export function entryVerdictFromDetail(detail, questionId, requestId) {
  const block = (detail?.messages || [])
    .flatMap(message => (message?.role === 'assistant' ? message.blocks || [] : []))
    .find(candidate => candidate?.type === 'question' && candidate.question_id === questionId)
  const state = block?.request_states?.[requestId]
  if (state && typeof state === 'object' && state.status) return { kind: 'settled', state }
  if (detail?.open_request_card_id !== questionId) return { kind: 'gone' }
  if (block && (block.questions || []).some(entry => entry?.id === requestId) && !block.answers) {
    return { kind: 'open' }
  }
  return { kind: 'unknown' }
}


/**
 * After a press with no verdict: read the chat once (`readDetail` resolves to
 * the chat detail) and say what the server recorded. Never resends anything.
 */
export async function reconcileUnknownPress({ readDetail, questionId, requestId }) {
  try {
    return entryVerdictFromDetail(await readDetail(), questionId, requestId)
  } catch {
    return { kind: 'unknown' }
  }
}


/**
 * What a Restart now press that failed or was refused amounts to, from the
 * server's own record (`readVerdict` resolves to reconcileUnknownPress's
 * verdict and never throws). `{ state }` when the restart was recorded after
 * all (this press or an earlier one): the entry shows it, with no error.
 * Otherwise `{ error }`, the original failure marked with the verdict
 * (`reconciled`: open, gone or unknown) that its copy is chosen from.
 */
export async function restartPressFailureOutcome(error, readVerdict) {
  const failure = error instanceof Error ? error : Object.assign(new Error('Restart press failed'), error || {})
  const verdict = await readVerdict()
  if (verdict?.kind === 'settled' && verdict.state) return { state: verdict.state }
  failure.reconciled = ['open', 'gone'].includes(verdict?.kind) ? verdict.kind : 'unknown'
  return { error: failure }
}
