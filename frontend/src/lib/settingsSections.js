// The stable ids of the Settings sections the shell can focus.
//
// One vocabulary for every way to reach a section: a mini-app's
// `moebius:open-settings` request, the agent's `open_item` with kind
// `settings`, and the `/shell/?settings=<id>` notification/deep-link target.
// The ids name what the owner sees, not the layout, so they survive Settings
// being reorganized; SettingsView maps each one to its current anchor.
// backend/app/routes/notify.py keeps the same closed set for its 422 check
// (pinned by backend/tests/test_notify_open_item.py), so add an id in both.
export const SETTINGS_SECTIONS = Object.freeze([
  'ai-providers',
  'background-agents',
  'github',
  'keys',
  'models',
])

const KNOWN = new Set(SETTINGS_SECTIONS)

/** A known section id, or null for Settings without a focused section. */
export function settingsSectionOrNull(value) {
  return typeof value === 'string' && KNOWN.has(value) ? value : null
}
