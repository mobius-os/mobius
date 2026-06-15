// Resolve the portable briefing text from a `kind === 'compaction'` message,
// for CompactionCard to render. The briefing lives in the plain-text
// `content` field — the SAME field chat.py's `_latest_compaction_brief`
// replays into the next provider's context — so reading `content` first
// keeps the rendered text and the replayed text in lockstep.
//
// Fallback order covers legacy messages: very old compactions stored the
// summary only inside a `CompactChat` tool block's `output` (or a plain
// text block), with `content` sometimes empty. Falling back to those means
// pre-existing chats still surface their briefing after the reframe, with
// no backend migration.
export function compactionBrief(msg) {
  const content = msg?.content
  if (typeof content === 'string' && content.trim()) return content
  const blocks = Array.isArray(msg?.blocks) ? msg.blocks : []
  const tool = blocks.find(block => block.type === 'tool')
  if (tool && typeof tool.output === 'string' && tool.output.trim()) {
    return tool.output
  }
  const text = blocks.find(block => block.type === 'text')
  if (text && typeof text.content === 'string' && text.content.trim()) {
    return text.content
  }
  return ''
}
