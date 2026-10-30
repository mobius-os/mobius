/** Resolve typed/selected input identically for single and multi-select cards. */
export function resolveQuestionAnswer(answer, otherText) {
  if (Array.isArray(answer)) {
    return answer.map(value => value === '__other__' ? otherText?.trim() || '' : value)
      .filter(Boolean).join(', ')
  }
  if (answer === '__other__') return otherText?.trim() || ''
  return answer || ''
}

/**
 * The group of a restored draft file whose question the card no longer asks.
 * It stays card-level and is sent untagged, but never answers a question, not
 * even a single-question card's. NUL keeps it apart from any question text.
 */
export const ORPHANED_FILE_GROUP = '\u0000orphaned'

/** An untagged legacy file has an unambiguous answer only on a single-question card. */
export function fileBelongsToQuestion(tag, question, questionCount) {
  return tag === question || (questionCount === 1 && tag == null)
}

/** The server `question` tag for a file group; orphaned and legacy files have none. */
export function fileQuestionTag(group) {
  return group == null || group === ORPHANED_FILE_GROUP ? undefined : group
}

/**
 * A restored draft file tagged for a question the card no longer asks would be
 * hidden yet still submitted, and the server refuses it; mark it orphaned.
 */
export function restoredQuestionFiles(files, questions) {
  const asked = new Set(questions.map(question => question.question))
  return files.map(file => (file.group == null || asked.has(file.group) ? file : { ...file, group: ORPHANED_FILE_GROUP }))
}

/**
 * Every question needs its own answer: text, a choice, or files belonging to
 * that question. Untagged legacy files count only when there is one question.
 */
export function questionAnswersReady(questions, answers, otherTexts, files) {
  return unansweredQuestions(questions, answers, otherTexts, files).length === 0
}

/** The questions that still need an answer, in card order. */
export function unansweredQuestions(questions, answers, otherTexts, files) {
  return questions.filter(question => !(
    Boolean(resolveQuestionAnswer(answers[question.question], otherTexts[question.question]))
    || files.some(file => fileBelongsToQuestion(file.group, question.question, questions.length))
  ))
}

/** Derive explicit saved-option choices without interpreting custom answer text. */
export function questionOptionSubmission(questions, answers) {
  const selected_options = {}
  let closeOnlySelection = questions.length > 0
  for (const question of questions) {
    const answer = answers[question.question]
    const labels = Array.isArray(answer) ? answer : (answer ? [answer] : [])
    const choices = labels.map(label => label === '__other__'
      ? null
      : question.options?.find(option => option.label === label))
    // IDs describe the complete answer to one subquestion, not just a known
    // subset. Mixed custom input stays a normal answer; sending partial IDs
    // would falsely claim the text exactly matches those saved choices.
    const explicitOptions = choices.length > 0
      && choices.every(option => typeof option?.id === 'string')
    if (question.id && explicitOptions) {
      selected_options[question.id] = choices.map(option => option.id)
    }
    closeOnlySelection &&= Boolean(question.id) && explicitOptions
      && choices.every(option => option.on_answer === 'close')
  }
  return { selected_options, closeOnlySelection }
}


/** One authoritative answer receipt survives local patching and stream replay. */
export function questionAnswerPatch(answers, disposition = {}) {
  return {
    answers,
    ...Object.fromEntries(['answer_turn', 'selected_options', 'platform_action', 'attachments']
      .filter(key => disposition?.[key] !== undefined)
      .map(key => [key, disposition[key]])),
  }
}
