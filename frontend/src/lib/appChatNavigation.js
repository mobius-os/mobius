/* App-requested chat navigation. An app may open a conversation and leave an
   editable draft there; it never replaces the owner's text or sends anything. */
import { probeDeletion } from '../api/client.js'
import { persistComposerDraft, readComposerDraft, readComposerDraftAsync } from '../components/ChatView/composerDraft.js'
import { loadFailedSendAttempt } from '../components/ChatView/sendAttemptRecovery.js'

/** Add an app's draft after the owner's unsent text, keeping their files.
 *  It is saved as the chat's own draft (no composer handoff, so never a send),
 *  and repeating the same draft adds nothing. Resolves to the combined draft,
 *  or undefined when the app sent none. A failed send awaiting resolution owns
 *  the composer, so it refuses then. */
export async function stageAppChatDraft(chatId, draft) {
  if (typeof draft !== 'string' || !draft) return undefined
  await readComposerDraftAsync(chatId)
  if (loadFailedSendAttempt(chatId)) {
    throw new Error('Open this conversation and resolve its previous message before adding a draft.')
  }
  const saved = readComposerDraft(chatId)
  const input = !saved.input || saved.input === draft || saved.input.endsWith(`\n\n${draft}`)
    ? saved.input || draft
    : `${saved.input}\n\n${draft}`
  if (!persistComposerDraft(chatId, input, saved.attachments)) {
    throw new Error('The draft could not be saved. Open the conversation and try again.')
  }
  return input
}

/** Open a chat with an app's draft added there. When that chat cannot take
 *  it (see stageAppChatDraft), the draft starts a new chat instead of being
 *  dropped. `open` receives the combined draft, or undefined without one. */
export async function openChatWithAppDraft(chatId, draft, { open, openNew }) {
  let combined
  try {
    combined = await stageAppChatDraft(chatId, draft)
  } catch {
    return openNew(draft)
  }
  return open(combined)
}

/** `moebius:open-existing-chat`: open exactly this chat or report why not.
 *  Never a new-chat fallback, and nothing is staged for a missing target. */
export async function openExistingChat(request, navigate) {
  const target = await probeDeletion(`/chats/${encodeURIComponent(request.chatId)}?limit=1&compact=1`)
  if (target !== 'exists') return { outcome: target === 'deleted' ? 'target_missing' : 'unavailable' }
  let draft
  try { draft = await stageAppChatDraft(request.chatId, request.draft) } catch { return { outcome: 'unavailable' } }
  navigate(draft)
  return { outcome: 'opened' }
}
