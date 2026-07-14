import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import MarkerCard from './MarkerCard.jsx'
import { compactionBrief } from './compactionToolBlock.js'

// A compaction message is a product moment, not a tool call: the chat's
// earlier context was condensed into a portable briefing (so work can
// continue after a cross-provider switch). The generic ToolBlock rendered
// it as "CompactChat: POST /api/chats/{id}/compact", which read as leaked
// plumbing. This card reframes it as a labeled divider — "Conversation
// summarized" — with the briefing tucked behind a chevron.
//
// The briefing text is read from the plain-text `content` field (via
// compactionBrief), which is the SAME field chat.py's
// `_latest_compaction_brief` replays into the next provider's context.
// Rendering never touches the stored shape, so the round-trip stays intact.
export default function CompactionCard({ msg }) {
  const brief = compactionBrief(msg)
  // Atomic provider handoffs attach `to_provider`; older compaction rows have
  // neither field and degrade to the plain label.
  const provider = providerLabel(msg)
  const subtitle = provider ? `before switching to ${provider}` : null

  // Renders through the shared MarkerCard shell. When `brief` is empty the
  // shell drops the chevron and stays a static labeled divider on its own.
  return (
    <MarkerCard title="Conversation summarized" subtitle={subtitle} icon={
      /* Two arrows converging into one line — "context condensed". */
      <svg viewBox="0 0 16 16" width="14" height="14" fill="none"
        stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"
        strokeLinejoin="round">
        <path d="M2 4h12M4 8h8M6 12h4" />
      </svg>
    }>
      {brief ? <StandardMarkdown text={brief} /> : null}
    </MarkerCard>
  )
}

// Pull a readable provider name off new handoff rows while preserving old rows.
function providerLabel(msg) {
  const raw = msg?.to_provider || msg?.provider
  if (typeof raw !== 'string' || !raw.trim()) return null
  const known = { claude: 'Claude Code', codex: 'OpenAI Codex' }
  return known[raw.trim().toLowerCase()] || raw.trim()
}
