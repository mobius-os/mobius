import { useState } from 'react'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
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
// Rendering never touches the stored shape, so the round-trip stays intact;
// legacy messages that only carry the tool block still resolve because
// compactionBrief falls back to the block output.
export default function CompactionCard({ msg }) {
  const [open, setOpen] = useState(false)
  const brief = compactionBrief(msg)
  // `provider` isn't part of the stored compaction shape today, so this
  // context line only appears if a future writer attaches it — the card
  // degrades to the plain "Conversation summarized" label otherwise.
  const provider = providerLabel(msg)
  const subtitle = provider ? `before switching to ${provider}` : null

  return (
    <div className={`chat__compaction${open ? ' chat__compaction--open' : ''}`}>
      <button
        type="button"
        className="chat__compaction-header"
        onClick={() => brief && setOpen(o => !o)}
        aria-expanded={brief ? open : undefined}
        disabled={!brief}
      >
        <span className="chat__compaction-icon" aria-hidden="true">
          {/* Two arrows converging into one line — "context condensed". */}
          <svg viewBox="0 0 16 16" width="14" height="14" fill="none"
            stroke="currentColor" strokeWidth="1.4" strokeLinecap="round"
            strokeLinejoin="round">
            <path d="M2 4h12M4 8h8M6 12h4" />
          </svg>
        </span>
        <span className="chat__compaction-label">
          <span className="chat__compaction-title">Conversation summarized</span>
          {subtitle && (
            <span className="chat__compaction-sub">{subtitle}</span>
          )}
        </span>
        {brief && (
          <span className="chat__compaction-toggle" aria-hidden="true">
            {open ? '▾' : '▸'}
          </span>
        )}
      </button>
      {open && brief && (
        <div className="chat__compaction-brief">
          <StandardMarkdown text={brief} />
        </div>
      )}
    </div>
  )
}

// Pull a provider name off the message if one was ever attached. The stored
// compaction message has no provider field today (chat_writer's
// `_persist_compaction`), so this returns null in practice — the card simply
// omits the context line. Kept so the card lights up automatically if the
// stored shape later grows a provider hint, without another frontend change.
function providerLabel(msg) {
  const raw = msg?.to_provider || msg?.provider
  if (typeof raw !== 'string' || !raw.trim()) return null
  const known = { claude: 'Claude Code', codex: 'OpenAI Codex' }
  return known[raw.trim().toLowerCase()] || raw.trim()
}
