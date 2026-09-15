/**
 * Stateful chat-runtime fixture shared by intercepted browser tests.
 *
 * Production orders every chat-detail and /runtime projection with one
 * database-owned lifecycle revision.  A fixture must therefore return the
 * same snapshot through both routes and advance the revision whenever its
 * simulated lifecycle changes; otherwise ChatView correctly rejects the
 * response as unordered.
 */
const DEFAULT_RUNTIME = Object.freeze({
  running: false,
  run_id: null,
  run_status: null,
  active_assistant_message_id: null,
  recovery_run_id: null,
  active_goal_objective: null,
  goal: null,
  pending_messages: [],
  pending_question_id: null,
  updated_at: null,
  waits: [],
  background_helpers: [],
})

function normalizedState(value, fallbackRunId) {
  const state = { ...DEFAULT_RUNTIME, ...value }
  if (state.running) {
    state.run_id ||= fallbackRunId
    state.run_status = 'running'
  } else if (state.run_id && !state.run_status) {
    state.run_status = 'completed'
  }
  return state
}

export function createMockChatRuntime(initial = {}) {
  let revision = Number.isSafeInteger(initial.runtime_revision)
    ? initial.runtime_revision
    : 0
  const fallbackRunId = initial.run_id || 'fixture-run'
  let state = normalizedState(initial, fallbackRunId)
  delete state.runtime_revision

  const snapshot = (overrides = {}) => ({
    ...normalizedState({ ...state, ...overrides }, fallbackRunId),
    runtime_revision: revision,
  })

  return {
    snapshot,
    detail(detail = {}) {
      // Runtime fields own the overlap so detail and /runtime cannot disagree.
      return { ...detail, ...snapshot() }
    },
    update(patch = {}) {
      revision += 1
      const transition = { ...state, ...patch }
      if (patch.running === false && patch.run_status === undefined) {
        transition.run_status = transition.run_id ? 'completed' : null
      }
      state = normalizedState(transition, fallbackRunId)
      return snapshot()
    },
    get revision() { return revision },
  }
}
