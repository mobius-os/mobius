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


function savedDraftFiles(files) {
  return files.filter(file => file.status === 'done').map(({ name, size, mime_type, group }) => ({ name, size, mime_type, group, status: 'done' }))
}


export function writeQuestionDraft(key, { answers, otherTexts, files = [] }, storage) {
  if (!key) return
  const hasAnswers = Object.keys(answers || {}).length > 0
  const hasText = Object.values(otherTexts || {}).some(value => String(value || '').length > 0)
  const savedFiles = savedDraftFiles(files)
  writeDraftRecord(
    key,
    !hasAnswers && !hasText && savedFiles.length === 0
      ? null
      : { version: 1, answers, otherTexts, files: savedFiles },
    storage,
  )
}


/** Store one serialized draft record, or clear it everywhere when null. */
function writeDraftRecord(key, record, storage) {
  const targets = storage ? [storage] : browserDraftStorages()
  // Clearing is authoritative across every fallback. Returning after the
  // first successful remove leaves older session data available to resurrect.
  if (record === null) {
    for (const target of targets) {
      try { target.removeItem(key) } catch { /* blocked store */ }
    }
    return
  }

  const serialized = JSON.stringify(record)
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


// ── Request-list cards ────────────────────────────────────────────────
// A request-list card (block.input_requests) is republished as a new card
// whenever its turn ends, so a draft keyed by card id would be lost on every
// republication. Each request instead owns one draft record keyed by its
// stable request id, under the same per-chat prefix (so clearing a chat
// clears these too). The record names the request revision it was typed
// against: a changed revision means the question changed, so the old draft
// is dropped and the request is flagged as updated until it is answered.
//
// Inside a record, `choice` is the selection and `text` the typed answer
// (today a request has one field; a multi-field request adds one entry per
// field name). Files are stored without their card-local question group.

function requestDraftKey(chatId, requestId) {
  if (chatId == null || chatId === '' || !requestId) return null
  return `${QUESTION_DRAFT_PREFIX}${encodeURIComponent(String(chatId))}:request:${encodeURIComponent(String(requestId))}`
}


function readDraftRecord(key, storage) {
  const targets = storage ? [storage] : browserDraftStorages()
  for (const target of targets) {
    try {
      const parsed = JSON.parse(target.getItem(key) || 'null')
      if (parsed && typeof parsed === 'object') return parsed
    } catch { /* unreadable record: treat as absent */ }
  }
  return null
}


/**
 * The owner's unsent input for every request on a request-list card, in the
 * card-local shape QuestionCard edits (answers/otherTexts keyed by prompt,
 * files grouped by prompt), plus the ids whose question changed since the
 * draft was typed. Reading never writes: a superseded card may read without
 * disturbing the draft the current card owns.
 */
export function readRequestListDraft(chatId, questions, storage) {
  const draft = { answers: {}, otherTexts: {}, files: [], updated: [] }
  for (const q of questions || []) {
    const key = requestDraftKey(chatId, q?.id)
    if (!key) continue
    const record = readDraftRecord(key, storage)
    if (!record) continue
    if (record.revision !== q.request_revision) {
      // Typed against an earlier form of the question: its input is not an
      // answer to what is asked now.
      draft.updated.push(q.id)
      continue
    }
    if (record.updated) draft.updated.push(q.id)
    const fields = record.fields && typeof record.fields === 'object' ? record.fields : {}
    if (fields.choice !== undefined && fields.choice !== '') draft.answers[q.question] = fields.choice
    if (typeof fields.text === 'string' && fields.text) draft.otherTexts[q.question] = fields.text
    if (Array.isArray(record.files)) {
      draft.files.push(...record.files.map(file => ({ ...file, group: q.question })))
    }
  }
  return draft
}


/**
 * Save each request's unsent input under its request id. A request with no
 * input clears its record, except that the "updated" flag is kept so the
 * owner still sees that the question changed after switching away.
 */
export function writeRequestListDraft(chatId, questions, { answers = {}, otherTexts = {}, files = [], updated = [] }, storage) {
  const saved = savedDraftFiles(files)
  for (const q of questions || []) {
    const key = requestDraftKey(chatId, q?.id)
    if (!key) continue
    const choice = answers[q.question]
    const text = otherTexts[q.question] || ''
    const own = saved.filter(file => file.group === q.question)
      .map(({ group: _group, ...file }) => file)
    const hasChoice = Array.isArray(choice) ? choice.length > 0 : Boolean(choice)
    const isUpdated = updated.includes(q.id)
    writeDraftRecord(key, !hasChoice && !text && own.length === 0 && !isUpdated ? null : {
      version: 1,
      revision: q.request_revision,
      ...(isUpdated ? { updated: true } : {}),
      fields: { ...(hasChoice ? { choice } : {}), ...(text ? { text } : {}) },
      files: own,
    }, storage)
  }
}


/** Forget the drafts of requests that have been answered. */
export function clearRequestListDrafts(chatId, requestIds, storage) {
  for (const requestId of requestIds || []) {
    const key = requestDraftKey(chatId, requestId)
    if (key) writeDraftRecord(key, null, storage)
  }
}
