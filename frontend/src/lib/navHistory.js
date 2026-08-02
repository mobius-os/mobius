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
//   kind  — base | drawer | dismissible | app | nav. Drawer, transient
//           dismissibles, and reversible app entries carry the semantics needed
//           to consume Back without accidentally popping a shell route.
//
// The classic History store and Navigation API store are independent. Every
// write below is mirrored to both or NavigationEvent.destination.getState()
// would see shell entries as phantoms in modern Chromium. The mirror is
// best-effort — see mirrorCurrentEntry.

import { recordClientError } from './errorLog.js'

let _entrySequence = 0

function newEntryId() {
  try { if (crypto?.randomUUID) return crypto.randomUUID() } catch {}
  return `mobius-${Date.now()}-${++_entrySequence}`
}

export function navState(kind, {
  index = 0,
  route = null,
  entryId = null,
  appNav = null,
} = {}) {
  return {
    __mobiusNav: true,
    kind,
    index,
    route,
    ...(entryId ? { entryId } : {}),
    ...(appNav ? { appNav } : {}),
  }
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

// Canonical key for a per-pane app sentinel owner. A physical app history entry
// belongs to the (paneId, appId) that pushed it — NOT to the app id alone: a
// moved app keeps historical pane tags, and two visible apps can interleave
// physical entries, so keying counts only by app id would let one app's nav-pop
// decrement another's entry (design §5). JSON-array stringification is an
// unambiguous separator (a raw `${a}:${b}` collides on ids containing ':').
export function ownerKeyOf(paneId, appId) {
  return JSON.stringify([String(paneId), String(appId)])
}

// Pure "my tagged entry is topmost" predicate for the single-FIFO local-pop
// pump (design §5, contract §3.3.2). All seven conditions must hold at the
// instant before `history.back()`: (a) the caller passes the global queue head
// as `head`; (b) no local pop is in flight; (c) the drawer is not open; (d) the
// current tagged shell state is `kind:'app'`; (e) its entryId is the head's
// target; (f) that registry record is still `live` and its (paneId,appId)
// equals the head's ownerKey; (g) it is not already consumed/retired. Extracted
// as a pure function so the queue-until-topmost rule is unit-testable.
export function isTopmostAppEntry({ state, head, inFlight, drawerOpen, registry, consumed }) {
  if (!head) return false                                   // (a)
  if (inFlight) return false                                // (b)
  if (drawerOpen) return false                              // (c)
  if (!isMobiusNavState(state) || state.kind !== 'app') return false  // (d)
  const entryId = navEntryId(state)
  if (!entryId || entryId !== head.targetEntryId) return false        // (e)
  const rec = registry?.get(entryId)                        // (f)
  if (!rec || rec.status !== 'live') return false
  if (ownerKeyOf(rec.paneId, rec.appId) !== head.ownerKey) return false
  if (consumed?.has(entryId)) return false                  // (g)
  return true
}

// Pure selection of the entry a fresh nav-pop should target: the newest still-
// live physical entry for an app whose id is NOT already claimed by a queued or
// in-flight request (contract §3.3.1, H1). Without the exclusion, a double-tap
// before the first popstate lands enqueues the SAME newest entry twice — the
// second request can never reach topmost (its target consumes on the first) and
// wedges the FIFO head. `entries` is the registry in insertion order
// ([entryId, {appId, paneId, status}]); returns {entryId, paneId, appId} or null.
export function selectNavPopTarget(entries, appId, targetedEntryIds) {
  const target = String(appId)
  const claimed = targetedEntryIds instanceof Set
    ? targetedEntryIds
    : new Set(targetedEntryIds || [])
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    const [entryId, rec] = entries[i]
    if (rec.appId === target && rec.status === 'live' && !claimed.has(entryId)) {
      return { entryId, paneId: rec.paneId, appId: rec.appId }
    }
  }
  return null
}

// Drop every queued local-pop request that targets `entryId`. When an ordinary
// shell Back consumes a hidden app's physical sentinel directly (handleBack
// branch 4), the app's still-QUEUED nav-pop for that same entry becomes dead:
// its target can never be topmost again, so it would wedge the single global
// FIFO head and block every later app's pop (finding: FIFO wedge on ordinary
// Back). Pruning the satisfied request when its entry is consumed keeps the
// pump able to advance. Pure; returns the SAME array when nothing was dropped.
export function dropPopsForEntry(queue, entryId) {
  if (!Array.isArray(queue) || entryId == null) return queue
  const next = queue.filter((req) => req.targetEntryId !== entryId)
  return next.length === queue.length ? queue : next
}

// The Navigation-store mirror is BEST-EFFORT. The classic History write that
// precedes every call below is the authoritative store: all shell logic reads
// history.state (navEntryIndex), and WebKit traversals resolve through the
// popstate fallback. The mirror exists only so Chromium's
// NavigationEvent.destination.getState() doesn't see shell entries as
// phantoms.
//
// WebKit (iOS 18.4+ ships the Navigation API) can wedge that API into a state
// where `updateCurrentEntry` throws InvalidStateError from native code on
// EVERY call until the page reloads (observed in the field on an iOS
// standalone-PWA install, 2026-07-29). Unguarded, that throw escaped AFTER
// history.pushState had already run, killing openDrawer mid-flight on every
// tap — the drawer opened once per fresh launch and never again, and every
// other nav-writing tap died the same way. A failed mirror must never take
// down the interaction that triggered it: swallow the throw and report it
// through the debounced client-error channel so the broken engine state
// stays diagnosable rather than silently absorbed.
function mirrorCurrentEntry(state) {
  if (typeof navigation === 'undefined' || !navigation.updateCurrentEntry) return
  try {
    navigation.updateCurrentEntry({ state })
  } catch (error) {
    recordClientError({ where: 'navHistory.mirrorCurrentEntry', error })
  }
}

export function pushNavEntry(kind, route = null, {
  currentState = history.state,
  appNav = null,
  entryId = null,
} = {}) {
  const current = navEntryIndex(currentState)
  const state = navState(kind, {
    index: current == null ? 0 : current + 1,
    route,
    // A caller normally gets a fresh identity. The one deliberate override is
    // useNavigation re-arming the SAME live dismissible after an older close's
    // delayed bookkeeping traversal crossed it. The old physical copy is on
    // the discarded Forward branch; preserving the logical id keeps the
    // mounted surface's close handle correlated with its replacement sentinel.
    entryId: typeof entryId === 'string' ? entryId : newEntryId(),
    appNav,
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
export function updateCurrentNavEntry(route, options = {}) {
  const { kind } = options
  if (!isMobiusNavState(history.state)) return null
  const state = {
    ...history.state,
    kind: kind || history.state.kind,
    route,
  }
  // Omitted means preserve existing correlation. `null` deliberately retires
  // it when a reversible app entry cannot be reconstructed after Forward.
  if (Object.prototype.hasOwnProperty.call(options, 'appNav')) {
    if (options.appNav) state.appNav = options.appNav
    else delete state.appNav
  }
  // Re-keying is reserved for the same dismissible re-arm described above.
  // Ordinary route refreshes never pass this option and preserve identity.
  if (Object.prototype.hasOwnProperty.call(options, 'entryId')) {
    if (typeof options.entryId === 'string') state.entryId = options.entryId
    else delete state.entryId
  }
  history.replaceState(state, '')
  mirrorCurrentEntry(state)
  return state
}
