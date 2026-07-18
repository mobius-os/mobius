const QUESTION_DRAFT_PREFIX = 'qa-draft:'


function browserDraftStorages() {
  // A question choice is unfinished user input, not disposable view state.
  // Android may recreate a standalone PWA after a long offline/background
  // spell, which drops sessionStorage even though the chat itself comes back.
  // Keep the draft in durable origin storage; fall back to sessionStorage for
  // restricted/private contexts where localStorage is unavailable.
  const stores = []
  try {
    if (globalThis.localStorage) stores.push(globalThis.localStorage)
  } catch { /* storage blocked */ }
  try {
    if (globalThis.sessionStorage && !stores.includes(globalThis.sessionStorage)) {
      stores.push(globalThis.sessionStorage)
    }
  } catch { /* storage blocked */ }
  return stores
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
  if (!key) return { answers: {}, otherTexts: {} }
  const targets = storage ? [storage] : browserDraftStorages()
  for (const target of targets) {
    try {
      const raw = target.getItem(key)
      if (!raw) continue
      const parsed = JSON.parse(raw)
      return {
        answers: parsed?.answers && typeof parsed.answers === 'object'
          ? parsed.answers
          : {},
        otherTexts: parsed?.otherTexts && typeof parsed.otherTexts === 'object'
          ? parsed.otherTexts
          : {},
      }
    } catch { /* malformed or blocked store; try the fallback */ }
  }
  return { answers: {}, otherTexts: {} }
}


export function writeQuestionDraft(
  key,
  answers,
  otherTexts,
  storage,
) {
  if (!key) return
  const targets = storage ? [storage] : browserDraftStorages()
  const hasAnswers = Object.keys(answers || {}).length > 0
  const hasText = Object.values(otherTexts || {}).some(value => String(value || '').length > 0)
  for (const target of targets) {
    try {
      if (!hasAnswers && !hasText) {
        target.removeItem(key)
      } else {
        target.setItem(key, JSON.stringify({ answers, otherTexts }))
      }
      return
    } catch {
      // A storage object can exist but reject writes (notably some private
      // browsing modes). Try the next origin store before giving up.
    }
  }
}


export function clearQuestionDraft(key, storage) {
  if (!key) return
  const targets = storage ? [storage] : browserDraftStorages()
  for (const target of targets) {
    try { target.removeItem(key) } catch { /* private browsing */ }
  }
}


export function clearChatQuestionDrafts(chatId, storage) {
  if (chatId == null || chatId === '') return
  const prefix = `${QUESTION_DRAFT_PREFIX}${encodeURIComponent(String(chatId))}:`
  const targets = storage ? [storage] : browserDraftStorages()
  for (const target of targets) {
    try {
      const matches = []
      for (let i = 0; i < target.length; i++) {
        const key = target.key(i)
        if (key?.startsWith(prefix)) matches.push(key)
      }
      for (const key of matches) target.removeItem(key)
    } catch { /* private browsing */ }
  }
}
