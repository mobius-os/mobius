// Tab model — the openable-item primitive behind the shell's tab strip.
//
// A tab is a pinned reference to a chat or app the owner can swap to:
// `{ kind: 'chat' | 'app', id: string }`. This module owns the whole tab
// contract — construction, identity, the open-set invariants (dedup + cap),
// persistence, how a tab maps to navigation, and whether it is the one on
// screen — so no call site has to re-derive them.
//
// It is deliberately dependency-free and pane-agnostic. Today the shell keeps
// ONE open set (a flat strip) and one on-screen view. The planned multi-pane
// workspace (build several apps / chat with several agents in tiled panes)
// holds a set of these tabs PER pane and computes active-ness against that
// pane's own view — the same primitives, fed pane state instead of global
// nav. See docs/design/multi-pane-workspace.md for the target model and the
// scroll/nav constraints that migration must honor.

// Cap the open set so the strip stays legible on a phone; the workspace can
// raise or drop this per pane later.
export const MAX_TABS = 6

// Ids are stored as strings for stable React keys + sessionStorage. App ids
// are re-coerced to Number in tabNavTarget — the ONLY correct nav shape (the
// iframe LRU dedups on strict !==, so a string id would double-mount).
export function makeTab(kind, id) {
  return { kind, id: String(id) }
}

export function sameTab(tab, kind, id) {
  return tab.kind === kind && tab.id === String(id)
}

// Stable per-tab identity — React key today, cross-pane reference later.
export function tabKey(tab) {
  return `${tab.kind}:${tab.id}`
}

// Add to the open set (idempotent), keeping the most-recent MAX_TABS.
export function addTab(tabs, kind, id) {
  return tabs.some(t => sameTab(t, kind, id))
    ? tabs
    : [...tabs, makeTab(kind, id)].slice(-MAX_TABS)
}

// Semantic chat -> artifact placement for today's single-strip workspace.
// The shell calls this when server truth says a chat produced a runnable app;
// it deliberately does NOT navigate, so the owner's current view/focus stays
// put. A future pane workspace keeps this operation at the call site and routes
// the app to the pane beside the chat instead of changing the event contract.
export function addBuiltAppForChat(tabs, chatId, appId) {
  if (tabs.some(t => sameTab(t, 'app', appId))) return tabs

  const next = [...tabs]
  let chatIndex = next.findIndex(t => sameTab(t, 'chat', chatId))
  if (chatIndex === -1) {
    next.push(makeTab('chat', chatId), makeTab('app', appId))
  } else {
    next.splice(chatIndex + 1, 0, makeTab('app', appId))
  }

  // Preserve the newly-related chat/app pair at the flat strip's cap. This is
  // intentionally local to the current projection; a pane will apply its own
  // per-pane capacity policy later.
  while (next.length > MAX_TABS) {
    chatIndex = next.findIndex(t => sameTab(t, 'chat', chatId))
    const appIndex = next.findIndex(t => sameTab(t, 'app', appId))
    const removable = next.findIndex((_, index) => index !== chatIndex && index !== appIndex)
    if (removable === -1) break
    next.splice(removable, 1)
  }
  return next
}

// Remove a tab — used both when the owner closes one and when its chat/app is
// deleted (so a stale tab can never navigate into a 404 / dead iframe).
export function removeTab(tabs, kind, id) {
  return tabs.filter(t => !sameTab(t, kind, id))
}

// The navTo(view, opts) target for opening a tab. App ids MUST be numeric:
// the iframe LRU dedups with a strict !==, so a string app id would sit beside
// the numeric one and mount the app twice. chatIds are strings throughout.
export function tabNavTarget(tab) {
  return tab.kind === 'app'
    ? { view: 'canvas', opts: { appId: Number(tab.id) } }
    : { view: 'chat', opts: { chatId: tab.id } }
}

// Is this tab the item currently shown? `view` is the { view, chatId, appId }
// nav focus. This maps today's single global focus to a tab; a pane in the
// workspace model instead compares by tabKey (docs/design/multi-pane-workspace.md).
export function isTabActive(tab, view) {
  return tab.kind === 'app'
    ? view.view === 'canvas' && String(view.appId) === tab.id
    : view.view === 'chat' && String(view.chatId) === tab.id
}

const STORAGE_KEY = 'mobius-open-tabs'

// Restore the open set from owner-written storage. Forgiving by design (a
// hand-edited or corrupt store must never crash the shell): drop malformed
// entries, drop app tabs whose id isn't a finite number (they would become
// NaN in tabNavTarget and never resolve), and dedup after normalizing so two
// id forms of the same tab can't render duplicate React keys.
export function readOpenTabs(storage = sessionStorage) {
  try {
    const parsed = JSON.parse(storage.getItem(STORAGE_KEY) || '[]')
    if (!Array.isArray(parsed)) return []
    const seen = new Set()
    const tabs = []
    for (const raw of parsed) {
      if (!raw || (raw.kind !== 'chat' && raw.kind !== 'app') || raw.id == null) continue
      if (raw.kind === 'app' && !Number.isFinite(Number(raw.id))) continue
      const tab = makeTab(raw.kind, raw.id)
      const key = tabKey(tab)
      if (seen.has(key)) continue
      seen.add(key)
      tabs.push(tab)
    }
    return tabs.slice(-MAX_TABS)
  } catch {
    return []
  }
}

export function writeOpenTabs(tabs, storage = sessionStorage) {
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(tabs))
  } catch {
    /* private mode / quota — tabs stay in memory only */
  }
}
