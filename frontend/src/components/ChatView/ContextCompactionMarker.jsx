/* A quiet timeline marker for provider-native context compaction. */

import './ContextCompactionMarker.css'

export default function ContextCompactionMarker() {
  // Intentionally not MarkerCard: native provider compaction exposes no
  // readable briefing and asks for no interaction. A borderless rule keeps it
  // chronological and visible without borrowing the accent card reserved for
  // deliberate conversation summaries and provider handoffs.
  return (
    <div className="chat__context-compaction" role="note" aria-label="Context compacted">
      <span className="chat__context-compaction-label">Context compacted</span>
    </div>
  )
}
