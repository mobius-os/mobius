// History-state tags for the shell's OWN session-history entries.
//
// A sandboxed mini-app or Web Studio preview iframe can push entries onto
// the SHARED top-level session history (an in-frame anchor or router that
// escapes the iframe). Those phantom entries previously desynced the
// shell's drawer/back-stack: the popstate handler treated a phantom as one
// of its own sentinels and over-popped navStackRef, so a device back-gesture
// would close the drawer or jump to the wrong view. Every entry the shell
// pushes now carries `{__mobiusNav: true, kind}`; the back handlers ignore
// pops that land on an UNTAGGED entry ("untagged == phantom"). The base
// entry is tagged too (via replaceState) so that invariant holds.
//
// See ARCHITECTURE.md (Navigation back-stack + drawer model): history-state
// tags guard against phantom descendant-frame entries. kind ∈ base | drawer | app | nav (informational
// only — the back handlers key off the __mobiusNav flag, not the kind).
export function navState(kind) {
  return { __mobiusNav: true, kind }
}

export function isMobiusNavState(state) {
  return !!(state && state.__mobiusNav === true)
}

// Push/replace a tagged session-history entry, writing the tag to BOTH the
// classic History API AND the Navigation API entry.
//
// The classic History store and the Navigation API store are INDEPENDENT:
// `history.pushState(state, '')` writes only the classic store, and the
// Navigation API's `e.destination.getState()` is blind to it (returns
// undefined). On a Navigation-API browser (modern Chrome, the installed PWA)
// the back/drawer handler's phantom guard reads `e.destination.getState()`,
// so a tag written ONLY via the classic API leaves every shell entry looking
// untagged → the guard suppresses every traversal → the drawer never closes
// and back-nav is dead. Mirroring the tag into the Navigation API entry via
// `navigation.updateCurrentEntry({ state })` makes `getState()` return the
// tag for the shell's own entries, so the guard passes them. Genuine phantom
// iframe entries are written to NEITHER store, so they stay untagged in both
// and the guard still suppresses them — phantom protection is preserved.
export function pushNavEntry(kind) {
  history.pushState(navState(kind), '')
  if (typeof navigation !== 'undefined' && navigation.updateCurrentEntry) {
    navigation.updateCurrentEntry({ state: navState(kind) })
  }
}

// url defaults to '' (current URL preserved). The base-entry call passes
// '/shell/' to reset a deep-link path to the manifest scope.
export function replaceNavEntry(kind, url = '') {
  history.replaceState(navState(kind), '', url)
  if (typeof navigation !== 'undefined' && navigation.updateCurrentEntry) {
    navigation.updateCurrentEntry({ state: navState(kind) })
  }
}
