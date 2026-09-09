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
    ...Object.fromEntries(['answer_turn', 'selected_options', 'platform_action']
      .filter(key => disposition?.[key] !== undefined)
      .map(key => [key, disposition[key]])),
  }
}
