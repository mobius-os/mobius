import { StandardMarkdown } from './markdown/BlockRenderer.jsx'
import { formatResetTime } from './resetTime.js'

// The single renderer for the error/pause/park card family, shared by BOTH
// surfaces that draw an error block: MsgContent (persisted transcript) and
// StreamingMessage (live stream + SSE catch-up). One renderer is the
// invariant — when the classification lived only in MsgContent, the live
// surface hardcoded the danger-red "Error" card and a benign pause flashed
// red until promotion. Any future field that changes how the card reads must
// land here, not in one surface.
//
// Classification: a provider-limit park carries `parked_until` and reads
// "Rate limit" — the honest, specific name a park deserves. A drain-gated
// restart or stall carries `pause_kind` ('restart' | 'stall') and reads
// "Paused". Both are WAIT states and get the soft `.chat__text--parked`
// treatment; the danger-red "Error" card is reserved for genuine failures.
// Old persisted blocks predate `pause_kind` and, absent a `parked_until`,
// fall back to the error rendering.
export function errorCardViewModel(block) {
  const parked = !!block.parked_until
  const benign = parked || !!block.pause_kind
  return {
    parked,
    className: `chat__text--error${benign ? ' chat__text--parked' : ''}`,
    label: parked ? 'Rate limit' : (block.pause_kind ? 'Paused' : 'Error'),
    resetLabel: parked ? formatResetTime(block.parked_until) : null,
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
