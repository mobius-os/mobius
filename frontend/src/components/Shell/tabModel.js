// Tab model — the openable-item primitive behind the shell's tab strip.
//
// A tab is a pinned reference to a chat or app the owner can swap to:
// `{ kind: 'chat' | 'app', id: string }`, plus the one canonical
// `{ kind: 'settings', id: 'settings' }` builder tab (see below). This module
// owns the whole tab contract — construction, identity, the open-set invariants
// (dedup + cap), persistence, how a tab maps to navigation, and whether it is
// the one on screen — so no call site has to re-derive them.
//
// It is deliberately dependency-free and pane-agnostic. Today the shell keeps
// ONE open set (a flat strip) and one on-screen view. The planned multi-pane
// workspace (build several apps / chat with several agents in tiled panes)
// holds a set of these tabs PER pane and computes active-ness against that
// pane's own view — the same primitives, fed pane state instead of global
// nav. See ARCHITECTURE.md's "Multi-pane workspace" section for the target
// model and the scroll/nav constraints that migration must honor.

// Cap the open set so the strip stays legible on a phone; the workspace can
// raise or drop this per pane later.
export const MAX_TABS = 6

// The Settings tab — a single-instance builder surface, not a chat/app.
//
// In builder mode (viewMode 'panes' with the feature enabled) Settings opens as
// a real tab in a pane instead of seizing the whole screen; in single mode it
// keeps today's full-screen takeover. As a tab it is CANONICAL: exactly one id
// ('settings'), so workspace-wide dedup gives single-instance behaviour for free
// (reopening focuses the existing tab, never a second one). Its id is a fixed
// string — unlike chat/app ids there is no per-instance identity — which is why
// `settingsTab()` takes no argument and `SETTINGS_TAB_KEY` is a constant.
//
// Deliberately kept OUT of the legacy `mobius-open-tabs` projection (readOpenTabs
// below drops any non-chat/app kind): that flat key is a one-release rollback
// mirror for shells that predate the Settings tab, so it must stay chat/app-only.
export const SETTINGS_ID = 'settings'
export const SETTINGS_TAB_KEY = 'settings:settings'
export function settingsTab() { return { kind: 'settings', id: SETTINGS_ID } }
export function isSettingsTab(tab) { return !!tab && tab.kind === 'settings' }

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

// The navTo(view, opts) target for opening a tab. App ids MUST be numeric:
// the iframe LRU dedups with a strict !==, so a string app id would sit beside
// the numeric one and mount the app twice. chatIds are strings throughout. The
// Settings tab carries no id payload — its destination is the mode-conditional
// Settings surface (tab in builder, overlay in single), resolved by the nav
// adapter's applySettingsDestination — so it yields `{ view: 'settings' }` with
// no `opts`.
export function tabNavTarget(tab) {
  if (tab.kind === 'settings') return { view: 'settings' }
  return tab.kind === 'app'
    ? { view: 'canvas', opts: { appId: Number(tab.id) } }
    : { view: 'chat', opts: { chatId: tab.id } }
}

const STORAGE_KEY = 'mobius-open-tabs'

// Restore the open set from owner-written storage. Forgiving by design (a
// hand-edited or corrupt store must never crash the shell): drop malformed
// entries, drop app tabs whose id isn't a finite number (they would become
// NaN in tabNavTarget and never resolve), and dedup after normalizing so two
// id forms of the same tab can't render duplicate React keys. The kind filter
// keeps this legacy projection chat/app-only — a Settings tab is never mirrored
// here (it lives only in the versioned workspace blob).
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
