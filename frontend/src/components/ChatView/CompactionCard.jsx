import { Collapse } from '@openai/apps-sdk-ui/components/Icon'
import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import MarkerCard from './MarkerCard.jsx'
import { compactionBrief } from './compactionToolBlock.js'
import { PROVIDER_INFO } from './providerRegistry.jsx'

// A compaction message is a product moment, not a tool call, so it renders as
// a labeled divider with its text tucked behind a chevron. Two kinds of row
// share it:
//   - a model-written briefing (from /compact or an older provider switch),
//     titled "Conversation summarized";
//   - a direct handover (`handover === 'direct'`), stored when the chat moves
//     to another provider: the saved digest and recent messages, word for
//     word, titled "Switched to {provider}".
//
// The text is read from the plain-text `content` field (via compactionBrief),
// which is the SAME field chat.py's `_latest_compaction_brief` replays into the
// next provider's context. Rendering never touches the stored shape.
export default function CompactionCard({ msg }) {
  const brief = compactionBrief(msg)
  const provider = providerLabel(msg)
  const { title, subtitle } = msg?.handover === 'direct'
    ? {
      title: provider ? `Switched to ${provider}` : 'Provider switched',
      subtitle: 'continuing from this chat\u2019s digest and recent messages',
    }
    : {
      title: 'Conversation summarized',
      subtitle: provider ? `before switching to ${provider}` : null,
    }

  // Renders through the shared MarkerCard shell. When `brief` is empty the
  // shell drops the chevron and stays a static labeled divider on its own.
  return (
    <MarkerCard title={title} subtitle={subtitle} icon={
      <Collapse width={18} height={18} aria-hidden="true" />
    }>
      {brief ? <StandardMarkdown text={brief} /> : null}
    </MarkerCard>
  )
}

// Pull a readable provider name off provider-switch rows while preserving old
// rows that carry neither field.
function providerLabel(msg) {
  const raw = msg?.to_provider || msg?.provider
  if (typeof raw !== 'string' || !raw.trim()) return null
  const id = raw.trim()
  return PROVIDER_INFO[id.toLowerCase()]?.label || id
}
