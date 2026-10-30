// A project's chat rows come from the project's own query, which is fetched
// when the project opens. The shell's chat list is the live source for the
// same chats: it learns the generated title, the first message, and the running
// state as they happen. Overlay those live facts so the project never shows a
// stale "New chat · Empty" while its agent is working.
export function liveProjectChat(row, liveRow) {
  if (!liveRow) return { ...row, running: !!row?.running }
  return {
    ...row,
    title: liveRow.title || row?.title || null,
    has_messages: !!(liveRow.has_messages || row?.has_messages),
    archived_at: liveRow.archived_at === undefined ? row?.archived_at : liveRow.archived_at,
    running: !!liveRow.running,
  }
}

// The single status word shown under a project chat's title, or null.
export function projectChatStatus(chat) {
  if (chat?.running) return 'Working…'
  if (chat?.archived_at) return 'Archived'
  if (!chat?.has_messages) return 'Empty'
  return null
}

// A pane tab is narrow: name it for the conversation, not the project, so the
// title survives truncation. An untitled chat still says which project it is in.
export function projectChatTabLabel({ title, projectName } = {}) {
  if (title) return title
  return projectName ? `${projectName} · New chat` : 'New chat'
}
