// Tab model — the openable-item primitive behind the shell's tab strip.
//
// A tab is a pinned reference to a chat or app the owner can swap to:
// `{ kind: 'chat' | 'app', id: string }`, plus the canonical shell-owned
// Apps and Settings tabs (see below). This module
// owns the whole tab contract — construction, identity, navigation mapping,
// and active-state comparison — so no call site has to re-derive them.
//
// It is deliberately dependency-free and pane-agnostic. The workspace holds
// these tabs per pane and computes active-ness against pane state.

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
export const SETTINGS_ID = 'settings'
export const SETTINGS_TAB_KEY = 'settings:settings'
export function settingsTab() { return { kind: 'settings', id: SETTINGS_ID } }
export function isSettingsTab(tab) { return !!tab && tab.kind === 'settings' }

// Apps is an ordinary, single-instance workspace destination. Unlike Settings
// it has no takeover/overlay mode: it opens wherever it is invoked, exactly
// like a chat or installed app, and workspace-wide tab dedup focuses the
// existing copy instead of creating another launcher.
export const APPS_ID = 'apps'
export const APPS_TAB_KEY = 'apps:apps'
export function appsTab() { return { kind: 'apps', id: APPS_ID } }
export function isAppsTab(tab) { return !!tab && tab.kind === 'apps' }

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
  if (tab.kind === 'apps') return { view: 'apps' }
  return tab.kind === 'app'
    ? { view: 'canvas', opts: { appId: Number(tab.id) } }
    : { view: 'chat', opts: { chatId: tab.id } }
}
