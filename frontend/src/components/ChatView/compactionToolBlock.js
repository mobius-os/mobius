export function compactionToolBlock(msg, chatId) {
  const blocks = Array.isArray(msg.blocks) ? msg.blocks : []
  const existingTool = blocks.find(block => block.type === 'tool')
  if (existingTool) {
    return { ...existingTool, defaultOpen: true }
  }
  const textBlock = blocks.find(block => block.type === 'text')
  return {
    type: 'tool',
    tool: 'CompactChat',
    input: chatId
      ? `POST /api/chats/${chatId}/compact`
      : 'POST /api/chats/{chat_id}/compact',
    output: msg.content || textBlock?.content || '',
    status: 'done',
    defaultOpen: true,
  }
}
