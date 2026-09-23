export async function readChatContinuityPage(chatId, afterRevision, apiFetch, signal) {
  const response = await apiFetch(
    `/chats/${chatId}/continuity?after_revision=${afterRevision}&limit=20&include_legacy=true`,
    { signal },
  )
  if (!response.ok) throw new Error(`Request failed (${response.status})`)
  return response.json()
}
