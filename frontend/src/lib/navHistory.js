// History-state tags for the shell's OWN session-history entries.
//
// A sandboxed mini-app or Web Studio preview iframe can push entries onto
// the SHARED top-level session history. Those entries are intentionally left
// untagged so the shell can ignore them. Shell entries carry three additional
// pieces of state:
//
//   index — the shell-relative session-history position. It lets the popstate
//           fallback distinguish Back from Forward without guessing.
//   route — the restorable shell view at this entry. Forward traversal cannot
//           be reconstructed from the destructive navStack alone.
//   kind  — base | drawer | app | nav. Unlike the original count-only model,
//           kind is now also used to restore a drawer on Forward; app remains
//           informational because a consumed app-local nested view cannot be
//           recreated by the host.
//
// The classic History store and Navigation API store are independent. Every
// write below is mirrored to both or NavigationEvent.destination.getState()
// would see shell entries as phantoms in modern Chromium.

let _entrySequence = 0

function newEntryId() {
  try { if (crypto?.randomUUID) return crypto.randomUUID() } catch {}
  return `mobius-${Date.now()}-${++_entrySequence}`
}

export function navState(kind, { index = 0, route = null, entryId = null } = {}) {
  return { __mobiusNav: true, kind, index, route, ...(entryId ? { entryId } : {}) }
}

export function isMobiusNavState(state) {
  return !!(state && state.__mobiusNav === true)
}

export function navEntryIndex(state) {
  return isMobiusNavState(state) && Number.isInteger(state.index)
    ? state.index
    : null
}

export function navEntryId(state) {
  return isMobiusNavState(state) && typeof state.entryId === 'string'
    ? state.entryId
    : null
}

export function navTraversalDirection(
  currentState,
  destinationState,
  { currentEntryIndex = null, destinationEntryIndex = null } = {},
) {
  const current = navEntryIndex(currentState)
  const destination = navEntryIndex(destinationState)
  if (current != null && destination != null && destination !== current) {
    return destination > current ? 'forward' : 'back'
  }
  if (Number.isInteger(currentEntryIndex) && Number.isInteger(destinationEntryIndex)) {
    if (destinationEntryIndex > currentEntryIndex) return 'forward'
    if (destinationEntryIndex < currentEntryIndex) return 'back'
    return 'same'
  }
  return current != null && destination === current ? 'same' : 'unknown'
}

export function isVisibleAppOwner(view, activeAppId, candidateAppId) {
  return view === 'canvas'
    && activeAppId != null
    && candidateAppId != null
    && String(activeAppId) === String(candidateAppId)
}

function mirrorCurrentEntry(state) {
  if (typeof navigation !== 'undefined' && navigation.updateCurrentEntry) {
    navigation.updateCurrentEntry({ state })
  }
}

export function pushNavEntry(kind, route = null, { currentState = history.state } = {}) {
  const current = navEntryIndex(currentState)
  const state = navState(kind, {
    index: current == null ? 0 : current + 1,
    route,
    entryId: newEntryId(),
  })
  history.pushState(state, '')
  mirrorCurrentEntry(state)
  return state
}

// url defaults to '' (current URL preserved). A base replacement starts a new
// shell-relative history model at index 0; other replacements retain position.
export function replaceNavEntry(kind, url = '', route = null) {
  const current = navEntryIndex(history.state)
  const state = navState(kind, {
    index: kind === 'base' || current == null ? 0 : current,
    route,
    entryId: newEntryId(),
  })
  history.replaceState(state, '', url)
  mirrorCurrentEntry(state)
  return state
}

// Route state changes after some pushes (notably Shell.newChat, which owns its
// state mutation). Refresh the current tagged entry without changing position.
// `kind` optionally promotes a consumed drawer sentinel to a semantic nav entry.
export function updateCurrentNavEntry(route, { kind } = {}) {
  if (!isMobiusNavState(history.state)) return null
  const state = {
    ...history.state,
    kind: kind || history.state.kind,
    route,
  }
  history.replaceState(state, '')
  mirrorCurrentEntry(state)
  return state
}
