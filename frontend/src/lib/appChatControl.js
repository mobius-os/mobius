/**
 * Authorize host-mediated control of an app-owned conversation once, then
 * remember the immutable ownership result for later status polls. The exact
 * AppCanvas window supplies appId; the app frame never supplies or receives
 * owner credentials.
 */
export function makeAppChatAuthorizer({ knownChats, loadChat } = {}) {
  const verified = new Set()

  return async function authorizeAppChat(appId, chatId) {
    const ownerId = String(appId)
    const id = String(chatId)
    const key = `${ownerId}:${id}`
    if (verified.has(key)) return true

    const known = typeof knownChats === 'function' ? knownChats() : []
    let chat = Array.isArray(known)
      ? known.find(row => String(row?.id) === id)
      : null
    if (chat && String(chat.created_by_app_id) !== ownerId) {
      throw new Error('That conversation does not belong to this app.')
    }
    if (!chat) {
      if (typeof loadChat !== 'function') {
        throw new Error('That conversation is unavailable.')
      }
      chat = await loadChat(id)
    }
    if (!chat || String(chat.created_by_app_id) !== ownerId) {
      throw new Error('That conversation does not belong to this app.')
    }
    verified.add(key)
    return true
  }
}

/**
 * Keep the owner-authorized app chat control outcome identical in workspace
 * and standalone hosts. Callers supply the chat client and response decoder;
 * this controller owns timeouts, best-effort Goal enrichment, and Stop.
 */
export function makeAppChatController({ knownChats, chats, readJson }) {
  const authorize = makeAppChatAuthorizer({
    knownChats,
    loadChat: async (id) => readJson(
      await chats.detail(id, { limit: 1, compact: true, timeoutMs: 5000 }),
      'Conversation check failed:',
    ),
  })

  return async function controlAppChat(appId, request) {
    await authorize(appId, request.chatId)
    if (request.action === 'stop') {
      return readJson(
        await chats.stop(request.chatId, { timeoutMs: 15000 }),
        'Could not stop the conversation:',
      )
    }
    const goalPlanPromise = chats.goalPlan(
      request.chatId,
      { timeoutMs: 5000 },
    ).then(async response => (
      response.ok ? (await response.json())?.plan ?? null : null
    )).catch(() => null)
    const usagePromise = chats.usage(
      request.chatId,
      { timeoutMs: 5000 },
    ).then(async response => (
      response.ok ? (await response.json()) ?? null : null
    )).catch(() => null)
    const runtime = await readJson(
      await chats.runtime(request.chatId, { timeoutMs: 5000 }),
      'Could not read conversation progress:',
    )
    return {
      ...runtime,
      goal_plan: await goalPlanPromise,
      usage: await usagePromise,
    }
  }
}
