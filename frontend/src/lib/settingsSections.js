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
  'models',
  // Whole sub-pages: Accounts › Möbius, and AI providers › Möbius (credit,
  // trial and Möbius models).
  'mobius-account',
  'mobius-models',
  // Settings › Notifications elsewhere (Slack, Discord, ntfy, webhooks).
  'notifications-elsewhere',
])

/** The sub-page a section id opens, or null for a section on the overview. */
export function settingsSectionPage(section) {
  if (section === 'mobius-account') return 'mobius-account'
  if (section === 'mobius-models') return 'mobius'
  if (section === 'notifications-elsewhere') return 'notifications-elsewhere'
  return null
}

const KNOWN = new Set(SETTINGS_SECTIONS)

/** A known section id, or null for Settings without a focused section. */
export function settingsSectionOrNull(value) {
  return typeof value === 'string' && KNOWN.has(value) ? value : null
}

/**
 * The section a mini-app's `moebius:open-settings` request focuses: the one it
 * names when known, otherwise AI providers.
 */
export function requestedSettingsSection(value) {
  return settingsSectionOrNull(value) ?? 'ai-providers'
}

/**
 * The `/shell/?settings=<id>` link that answers the same request from outside
 * the workspace (a standalone app page), focusing the same section.
 */
export function settingsDeepLink(value) {
  return `/shell/?${new URLSearchParams({ settings: requestedSettingsSection(value) })}`
}
