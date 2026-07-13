import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import { formatResetTime } from './resetTime.js'

// The single renderer for the error/pause/park card family. MsgContent consumes
// both persisted blocks and the converted live stream, so source selection
// cannot change this card's classification. When the live path had a separate
// renderer, a benign pause flashed danger-red until promotion. Any future field
// that changes how the card reads must land here.
//
// Classification, all from the single `pause` descriptor: a provider-limit
// park carries `pause.resets_at` and reads "Rate limit" — the honest, specific
// name a park deserves. A drain-gated restart or stall carries `pause.kind`
// ('restart' | 'stall') without a reset time and reads "Paused". Both are WAIT
// states (any `pause`) and get the soft `.chat__text--parked` treatment; the
// danger-red "Error" card is reserved for genuine failures (no `pause`). Old
// persisted blocks predate `pause` and fall back to the error rendering.
export function errorCardViewModel(block) {
  const parked = !!block.pause?.resets_at
  const benign = !!block.pause
  return {
    parked,
    className: `chat__text--error${benign ? ' chat__text--parked' : ''}`,
    label: parked ? 'Rate limit' : (block.pause ? 'Paused' : 'Error'),
    resetLabel: parked ? formatResetTime(block.pause.resets_at) : null,
  }
}

// `children` is the slot for surface-specific affordances — MsgContent
// appends its tail-gated Resume button there; the live surface renders none
// (a terminal error promotes within the same breath, and the button's
// tail-only gate is a persisted-transcript concept).
export default function ErrorCard({ block, children }) {
  const vm = errorCardViewModel(block)
  return (
    <div className={vm.className} role="alert">
      <span className="chat__error-label">{vm.label}</span>
      {/* StandardMarkdown so URLs in provider error payloads (quota links,
          billing pages) become clickable straight from the chat. */}
      <StandardMarkdown
        text={block.message || 'The agent ran into an issue.'}
      />
      {vm.parked && vm.resetLabel && (
        <div className="chat__parked-reset">Resets {vm.resetLabel}</div>
      )}
      {vm.parked && vm.resetLabel && (
        // Reassure that the wait resolves on its own: a reset push is coming.
        // Tapping Resume now before the reset just re-parks (the provider
        // limit is still in force), so name that honestly rather than letting
        // the button look broken.
        <div className="chat__parked-note">
          You'll get a notification when it resets — or tap Resume now to
          try sooner (it may pause again).
        </div>
      )}
      {children}
    </div>
  )
}
