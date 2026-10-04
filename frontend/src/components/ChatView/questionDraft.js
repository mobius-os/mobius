import { localStore, sessionStore } from '../../lib/workspaceStorage.js'

const QUESTION_DRAFT_PREFIX = 'qa-draft:'


function browserDraftStorages() {
  // A question choice is unfinished user input, not disposable view state.
  // Android may recreate a standalone PWA after a long offline/background
  // spell, which drops sessionStorage even though the chat itself comes back.
  // Keep the draft in durable storage; fall back to tab storage for
  // restricted/private contexts where durable storage is unavailable. A guest
  // has one grant store, which both accessors return.
  const stores = []
  for (const store of [localStore(), sessionStore()]) {
    if (store && !stores.includes(store)) stores.push(store)
  }
  return stores
}


function parsedDraft(storage, key) {
  try {
    const parsed = JSON.parse(storage.getItem(key) || 'null')
    if (!parsed) return null
    return {
      answers: parsed.answers && typeof parsed.answers === 'object'
        ? parsed.answers
        : {},
      otherTexts: parsed.otherTexts && typeof parsed.otherTexts === 'object'
        ? parsed.otherTexts
        : {},
      files: Array.isArray(parsed.files) ? parsed.files : [],
    }
  } catch {
    return null
  }
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
  if (!key) return { answers: {}, otherTexts: {}, files: [] }
  const targets = storage ? [storage] : browserDraftStorages()
  for (let index = 0; index < targets.length; index++) {
    const draft = parsedDraft(targets[index], key)
    if (!draft) continue

    // sessionStorage was the original home for question drafts. Migrate a
    // surviving legacy value into durable storage on first read, then remove
    // the fallback copy only after the durable write succeeds. Keeping two
    // writable copies lets a cleared local draft resurrect an old choice.
    if (!storage && index > 0 && targets[0]) {
      try {
        targets[0].setItem(key, JSON.stringify({ version: 1, ...draft }))
        targets[index].removeItem(key)
      } catch { /* durable storage unavailable; keep the working fallback */ }
    }
    return draft
  }
  return { answers: {}, otherTexts: {}, files: [] }
}


export function writeQuestionDraft(
  key,
  answers,
  otherTexts,
  storage,
  files = [],
) {
  if (!key) return
  const targets = storage ? [storage] : browserDraftStorages()
  const hasAnswers = Object.keys(answers || {}).length > 0
  const hasText = Object.values(otherTexts || {}).some(value => String(value || '').length > 0)
  const savedFiles = files.filter(file => file.status === 'done').map(({ name, size, mime_type }) => ({ name, size, mime_type, status: 'done' }))

  // Clearing is authoritative across every fallback. Returning after the
  // first successful remove leaves older session data available to resurrect.
  if (!hasAnswers && !hasText && savedFiles.length === 0) {
    for (const target of targets) {
      try { target.removeItem(key) } catch { /* blocked store */ }
    }
    return
  }

  const serialized = JSON.stringify({ version: 1, answers, otherTexts, files: savedFiles })
  for (const target of targets) {
    try {
      target.setItem(key, serialized)
      // Exactly one store owns the current value. Once a preferred write
      // succeeds, remove any legacy fallback so future reads are unambiguous.
      for (const fallback of targets) {
        if (fallback === target) continue
        try { fallback.removeItem(key) } catch { /* blocked fallback */ }
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
