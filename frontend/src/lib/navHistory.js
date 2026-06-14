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
// See docs/navigation.md "History-state tags guard against PHANTOM
// descendant-frame entries." kind ∈ base | drawer | app | nav (informational
// only — the back handlers key off the __mobiusNav flag, not the kind).
export function navState(kind) {
  return { __mobiusNav: true, kind }
}

export function isMobiusNavState(state) {
  return !!(state && state.__mobiusNav === true)
}
