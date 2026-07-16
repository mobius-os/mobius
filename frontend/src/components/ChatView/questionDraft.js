const QUESTION_DRAFT_PREFIX = 'qa-draft:'


function browserSessionStorage() {
  try { return globalThis.sessionStorage } catch { return null }
}


function questionFingerprint(questions) {
  const source = JSON.stringify((questions || []).map(q => ({
    header: q?.header || '',
    question: q?.question || '',
    multiSelect: !!q?.multiSelect,
    options: (q?.options || []).map(option => option?.label || ''),
  })))
  let hash = 2166136261
  for (let i = 0; i < source.length; i++) {
    hash ^= source.charCodeAt(i)
    hash = Math.imul(hash, 16777619)
  }
  return `legacy-${(hash >>> 0).toString(36)}`
}


export function questionDraftKey(chatId, questionId, questions) {
  if (chatId == null || chatId === '') return null
  const identity = questionId || questionFingerprint(questions)
  return `${QUESTION_DRAFT_PREFIX}${encodeURIComponent(String(chatId))}:${encodeURIComponent(String(identity))}`
}


export function readQuestionDraft(key, storage) {
  const target = storage ?? browserSessionStorage()
  if (!key || !target) return { answers: {}, otherTexts: {} }
  try {
    const parsed = JSON.parse(target.getItem(key) || 'null')
    return {
      answers: parsed?.answers && typeof parsed.answers === 'object'
        ? parsed.answers
        : {},
      otherTexts: parsed?.otherTexts && typeof parsed.otherTexts === 'object'
        ? parsed.otherTexts
        : {},
    }
  } catch {
    return { answers: {}, otherTexts: {} }
  }
}


export function writeQuestionDraft(
  key,
  answers,
  otherTexts,
  storage,
) {
  const target = storage ?? browserSessionStorage()
  if (!key || !target) return
  try {
    const hasAnswers = Object.keys(answers || {}).length > 0
    const hasText = Object.values(otherTexts || {}).some(value => String(value || '').length > 0)
    if (!hasAnswers && !hasText) {
      target.removeItem(key)
      return
    }
    target.setItem(key, JSON.stringify({ answers, otherTexts }))
  } catch { /* private browsing, disabled storage, or quota exceeded */ }
}


export function clearQuestionDraft(key, storage) {
  const target = storage ?? browserSessionStorage()
  if (!key || !target) return
  try { target.removeItem(key) } catch { /* private browsing */ }
}


export function clearChatQuestionDrafts(chatId, storage) {
  const target = storage ?? browserSessionStorage()
  if (chatId == null || chatId === '' || !target) return
  const prefix = `${QUESTION_DRAFT_PREFIX}${encodeURIComponent(String(chatId))}:`
  try {
    const matches = []
    for (let i = 0; i < target.length; i++) {
      const key = target.key(i)
      if (key?.startsWith(prefix)) matches.push(key)
    }
    for (const key of matches) target.removeItem(key)
  } catch { /* private browsing */ }
}
