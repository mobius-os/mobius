/* Pure state logic for the build-phase milestone rail in the chat foot.
 *
 * ChatView owns the side effects — subscribing to `build_phase` stream events,
 * rendering the rail, announcing new phases — while this module owns the
 * accumulate / dedupe / reset decisions so they can be tested without a DOM.
 *
 * A rail is an ordered list of the phases the building agent emitted in the
 * run being displayed, each `{ label, ts }`. Two transitions exist:
 *
 * - ACCUMULATE (`accumulateBuildPhase`) on a `build_phase` stream event,
 *   idempotent by `ts` so a catch-up replay never double-counts a phase.
 * - RESET (`railAtRunStart`) when a NEW RUN starts for this chat — a fresh
 *   send that starts a turn, or a queued message being promoted into its own
 *   turn (`queued_turn_starting`). NOTHING ELSE resets: merely ENQUEUING a
 *   send during a live run must leave the in-flight build's rail intact, and
 *   any mid-run reset would be silently undone by the next catch-up replay
 *   (the replayed current-run phases would repopulate what was cleared).
 *   Because a chat broadcast's event log spans exactly one run — and replays
 *   `queued_turn_starting` in order at the run boundary — resetting only at
 *   run start keeps every replay consistent with the rail on screen.
 */

// The empty rail is a shared frozen constant so a reset can hand back a stable
// reference (React skips a re-render when the value is identity-equal).
export const EMPTY_BUILD_PHASE_RAIL = Object.freeze([])

export function railAtRunStart() {
  // A new run is a new build context: the rail empties and repopulates from
  // the new run's own build_phase events (live or via its catch-up replay).
  return EMPTY_BUILD_PHASE_RAIL
}

export function buildPhaseFromEvent(event) {
  if (!event || event.type !== 'build_phase') return null
  const label = typeof event.label === 'string' ? event.label.trim() : ''
  const ts = Number(event.ts)
  // A phase needs a label to render and a finite ts to dedupe/key on; without
  // either it carries no rail signal, so it is dropped rather than shown blank.
  if (!label || !Number.isFinite(ts)) return null
  return { label, ts }
}

export function accumulateBuildPhase(rail, event) {
  const phase = buildPhaseFromEvent(event)
  if (!phase) return rail
  // Idempotent by ts: a catch-up replay of an already-seen phase is a no-op,
  // and returning the same array reference lets the caller skip a re-render.
  if (rail.some(p => p.ts === phase.ts)) return rail
  return [...rail, phase]
}

export function buildPhaseRailViewModel(rail) {
  const lastIndex = rail.length - 1
  return rail.map((phase, i) => ({
    ts: phase.ts,
    label: phase.label,
    current: i === lastIndex,
  }))
}

export function latestBuildPhaseAnnouncement(rail) {
  if (!Array.isArray(rail) || rail.length === 0) return ''
  return `Build phase: ${rail[rail.length - 1].label}`
}
