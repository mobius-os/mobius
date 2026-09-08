// Resolve the portable briefing text from a `kind === 'compaction'` message,
// for CompactionCard to render. The briefing lives in the plain-text
// `content` field — the SAME field chat.py's `_latest_compaction_brief`
// replays into the next provider's context — so reading `content` keeps the
// rendered text and the replayed text in lockstep.
export function compactionBrief(msg) {
  const content = msg?.content
  if (typeof content === 'string' && content.trim()) return content
  return ''
}
