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

export const PROJECTS_ID = 'projects'
export const PROJECTS_TAB_KEY = 'projects:projects'
export function projectsTab() { return { kind: 'projects', id: PROJECTS_ID } }
export function isProjectsTab(tab) { return !!tab && tab.kind === 'projects' }
export function projectTab(id) { return makeTab('project', id) }

// A project artifact (a website/latex build) opens as its OWN workspace tab —
// its own preview surface, distinct from the project browser it was opened
// from. One artifact tab needs TWO ids (which project, which artifact), so its
// tab id is the composite `<projectId>:<artifactId>`. A project id is numeric
// and an artifact id is a `[A-Za-z0-9_-]` slug, so neither half ever contains
// the ':' separator; split on the FIRST ':' to recover the pair.
export function artifactTabId(projectId, artifactId) {
  return `${String(projectId)}:${String(artifactId)}`
}
export function parseArtifactTabId(id) {
  const raw = String(id ?? '')
  const at = raw.indexOf(':')
  if (at === -1) return null
  const projectId = raw.slice(0, at)
  const artifactId = raw.slice(at + 1)
  if (!projectId || !artifactId) return null
  return { projectId, artifactId }
}
export function artifactTab(projectId, artifactId) {
  return makeTab('artifact', artifactTabId(projectId, artifactId))
}
export function isArtifactTab(tab) { return !!tab && tab.kind === 'artifact' }

// Ids are stored as strings for stable React keys + browser persistence. App ids
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
  if (tab.kind === 'projects') return { view: 'projects' }
  if (tab.kind === 'project') return { view: 'project', opts: { projectId: tab.id } }
  if (tab.kind === 'artifact') {
    const parsed = parseArtifactTabId(tab.id)
    return { view: 'artifact', opts: parsed || { projectId: null, artifactId: null } }
  }
  return tab.kind === 'app'
    ? { view: 'canvas', opts: { appId: Number(tab.id) } }
    : { view: 'chat', opts: { chatId: tab.id } }
}
