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
// name a park deserves. A drain-gated restart carries `pause.kind='restart'`
// without a reset time and reads "Paused". Both are WAIT
// states (any `pause`) and get the soft `.chat__text--parked` treatment; the
// danger-red "Error" card is reserved for genuine failures (no `pause`). Old
// persisted blocks predate `pause` and fall back to the error rendering.
export function errorCardViewModel(block) {
  const parked = !!block.pause?.resets_at
  const benign = !!block.pause
  return {
    parked,
    benign,
    className: `chat__text--error${benign ? ' chat__text--parked' : ''}`,
    label: parked ? 'Rate limit' : (block.pause ? 'Paused' : 'Error'),
    resetLabel: parked ? formatResetTime(block.pause.resets_at) : null,
  }
}

// `children` is the slot for surface-specific affordances — MsgContent
// appends its tail-gated Resume button there; the live surface renders none
// (a terminal error promotes within the same breath, and the button's
// tail-only gate is a persisted-transcript concept).
export default function ErrorCard({
  block,
  autoResume = false,
  resetElapsed = false,
  cardRef,
  children,
}) {
  const vm = errorCardViewModel(block)
  const recoveryTitle = vm.parked
    ? autoResume
      ? (vm.resetLabel ? `Queued to continue ${vm.resetLabel}` : 'Queued to continue')
      : resetElapsed
        ? 'Usage is available again'
        : (vm.resetLabel ? `Usage resets ${vm.resetLabel}` : 'Usage limit reached')
    : null
  const recoveryCopy = vm.parked
    ? autoResume
      ? 'Your work is safe. Möbius will continue automatically.'
      : resetElapsed
        ? 'Your work is safe. Continue when you’re ready.'
        : 'Your work is safe. Continue automatically when usage resets.'
    : null
  return (
    <div className={vm.className} ref={cardRef}>
      {/* Keep the announced status body separate from interactive children.
          Otherwise a switch update or nested save alert makes the atomic
          status region re-announce the whole rate-limit card. */}
      <div
        className="chat__error-status"
        role={vm.benign ? undefined : 'alert'}
      >
        {vm.parked ? (
          <>
            <div className="chat__recovery-title">{recoveryTitle}</div>
            <div className="chat__recovery-copy">{recoveryCopy}</div>
            {block.message && (
              <details className="chat__recovery-details">
                <summary>Technical details</summary>
                {/* Provider payloads sometimes carry useful quota links. Keep
                    them available without making internal codes the headline. */}
                <StandardMarkdown text={block.message} />
              </details>
            )}
          </>
        ) : (
          <>
            <span className="chat__error-label">{vm.label}</span>
            <StandardMarkdown
              text={block.message || 'The agent ran into an issue.'}
            />
          </>
        )}
      </div>
      {children}
    </div>
  )
}
