/* What a request-list card leaves in its reply once the agent carried on past it with nothing answered: an "Asked a question" activity row. */

import { APPROVAL, QUESTION, RESTART, SECRET, entryKind, entryState } from './requestEntries.js'

// The activity row's tool name: owner-language labels and the row itself come
// from toolActivityLabel.js and ToolBlock.jsx, like any other tool call.
export const ASKED_QUESTIONS_TOOL = 'AskedQuestions'

const KIND_LABELS = { [APPROVAL]: 'Approval', [RESTART]: 'Restart Möbius', [SECRET]: 'Secure input' }

function hasAnswers(answers) {
  return Boolean(answers) && typeof answers === 'object' && Object.keys(answers).length > 0
}

/**
 * Whether a saved card renders as the activity row instead of a card: a
 * request-list card the owner can no longer answer here, with nothing answered
 * or settled on it and no answer of this tab's still on its way. A card with
 * any answer or settled entry stays a card showing those.
 */
export function cardLeavesActivityRow(block, { answerable = false, locallyAnswered = false } = {}) {
  if (block?.type !== 'question' || block.input_requests !== true) return false
  if (answerable || locallyAnswered || hasAnswers(block.answers)) return false
  return !(block.questions || []).some(entry => entryState(block.request_states, entry))
}

/** The activity row standing for that card: what was asked, read-only. */
export function askedQuestionsActivity(block) {
  return {
    type: 'tool',
    tool: ASKED_QUESTIONS_TOOL,
    status: 'done',
    tool_use_id: `owner-card:${block.question_id}`,
    asked: (block.questions || []).map((entry, i) => {
      const kind = entryKind(entry)
      return {
        key: entry.id ?? String(i),
        label: KIND_LABELS[kind] || '',
        prompt: kind === SECRET ? (entry.secure_input?.title || entry.question || '') : (entry.question || ''),
        options: kind === QUESTION || kind === APPROVAL
          ? (entry.options || []).map(option => ({ label: option.label || '', description: option.description || '' }))
          : [],
      }
    }),
  }
}

/** "Asked a question", or "Asked questions" for a card that held several. */
export function askedQuestionsLabel(tool) {
  return (tool?.asked?.length || 0) > 1 ? 'Asked questions' : 'Asked a question'
}
