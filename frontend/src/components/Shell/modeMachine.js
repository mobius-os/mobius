// Durable workspace mode is ordinary state. The only transient React projection
// left here is drag-preview: dragging a Standard tab temporarily reveals Builder
// before a valid drop commits it. Visual Standard ↔ Builder motion belongs to the
// browser's View Transition transaction (useModeViewTransition), not this reducer.

export function initialModeState(committedMode = 'panes') {
  return {
    committedMode: committedMode === 'single' ? 'single' : 'panes',
    transition: null,
    nextId: 1,
  }
}

export function modeReducer(state, event) {
  switch (event.type) {
    case 'drag-arm': {
      if (state.committedMode !== 'single') {
        return state.transition ? { ...state, transition: null } : state
      }
      const id = state.nextId
      return {
        committedMode: 'single',
        transition: {
          id,
          phase: 'drag-preview',
          from: 'single',
          to: 'single',
          cause: 'drag',
        },
        nextId: id + 1,
      }
    }
    case 'drag-cancel': {
      const live = state.transition
      if (!live || live.phase !== 'drag-preview' || live.id !== event.id) return state
      return { ...state, transition: null }
    }
    case 'sync-committed': {
      const to = event.committedMode === 'single' ? 'single' : 'panes'
      if (to === state.committedMode && !state.transition) return state
      return { ...state, committedMode: to, transition: null }
    }
    default:
      return state
  }
}

export function effectiveViewMode(state) {
  if (state.transition?.phase === 'drag-preview') return 'panes'
  return state.committedMode
}

export function builderModeActive(state) {
  return state.committedMode === 'panes'
}

export function dragPreviewActive(state) {
  return state.transition?.phase === 'drag-preview'
}
