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
 * Card-level files can stand in for the answer only on a single-question card.
 * On a grouped card every question needs its own answer, so a file never
 * claims to have answered questions it says nothing about.
 */
export function questionAnswersReady(questions, answers, otherTexts, files) {
  const filesAnswer = questions.length === 1 && files.length > 0
  return questions.every(question => (
    Boolean(resolveQuestionAnswer(answers[question.question], otherTexts[question.question]))
    || filesAnswer
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
