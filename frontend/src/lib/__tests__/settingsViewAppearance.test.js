import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const view = readFileSync(
  new URL('../../components/SettingsView/SettingsView.jsx', import.meta.url),
  'utf8',
)
const css = readFileSync(
  new URL('../../components/SettingsView/SettingsView.css', import.meta.url),
  'utf8',
)

test('appearance keeps one icon switch without making the section clickable', () => {
  assert.match(view, /settings__section--appearance/)
  assert.match(view, /className="settings__appearance-toggle"[\s\S]*role="switch"[\s\S]*onClick=\{toggleTheme\}/)
  assert.match(view, /settings__appearance-option/)
  assert.doesNotMatch(view, /settings__appearance-thumb/)
  assert.match(view, /<Sun[\s\S]*<Moon/)
  assert.doesNotMatch(view, /<span>Light<\/span>|<span>Dark<\/span>|type="radio"/)
  assert.doesNotMatch(view, /<section[^>]*onClick=\{toggleTheme\}/)
  assert.match(css, /\.settings__appearance-toggle\s*\{[^}]*grid-template-columns:\s*repeat\(2, 34px\);/s)
})

test('model and synced commit use the same normal-weight standard highlight', () => {
  assert.match(view, /provider-row__status-text settings__last-model/)
  assert.match(view, /Choose which models appear\. New chats use your last pick\./)
  assert.match(view, /Last model: <span className="settings__standard-highlight">/)
  assert.match(view, /Synced to [\s\S]*settings__standard-highlight/)
  assert.doesNotMatch(view, /Serving local \{mobiusVersion\.localSha\}/)
  assert.match(css, /\.settings__last-model\s*\{[^}]*color:\s*var\(--muted\);[^}]*font-weight:\s*400;/s)
  assert.match(css, /\.settings__standard-highlight\s*\{[^}]*color:\s*var\(--green\);[^}]*font-weight:\s*inherit;/s)
})

test('background agents are always draggable without reorder chrome or a trailing caret', () => {
  assert.match(view, /settings-bg-row__effort-visual[\s\S]*settings-bg-row__effort-dot/)
  assert.match(view, /efforts=\{efforts\}[\s\S]*onEffortChange=\{onEffortChange\}/)
  assert.doesNotMatch(view, /settings-bg-row__effort-picker|<EffortStepper/)
  assert.doesNotMatch(view, /\{effortLabel\} effort<\/span>/)
  assert.match(view, /reorderMode\s*\n/)
  assert.match(view, /<GripVertical size=\{18\}/)
  assert.doesNotMatch(view, /settings-agent-group__reorder|>Reorder<|model-trigger__caret/)
  assert.match(view, /Background agents/)
  assert.match(view, /Used for memory, reflection, and other automatic tasks\. Tried in order\./)
  assert.match(css, /\.settings-bg-row\s*\{[^}]*border:\s*0;[^}]*background:\s*transparent;/s)
  assert.match(css, /\.settings-bg-row__effort-visual\s*\{[^}]*min-width:\s*68px;/s)
  assert.doesNotMatch(view, /dropPosition|settings-bg-row--drop-before|settings-bg-row--drop-after/)
  assert.doesNotMatch(css, /settings-bg-row--drop-before|settings-bg-row--drop-after/)
})

test('new provider connections use the curated unattended defaults', () => {
  assert.match(view, /claude: 'claude-opus-4-8'/)
  assert.match(view, /codex: 'gpt-5\.6-terra'/)
  assert.match(view, /authProvidersAtStartRef\.current = new Set\(configuredProvidersRef\.current\)/)
  assert.match(view, /const newlyConnected = !providersBefore\.has\(provider\)/)
  assert.match(view, /providersBefore\.size === 0[\s\S]*connectedRow[\s\S]*enabled: false/)
  assert.match(view, /const onProviderConnected = useCallback\(async \(provider\)/)
  assert.match(view, /await persistBackgroundAgents\([\s\S]*providersBefore\.size === 0 \? \{ provider \} : \{\}/)
  assert.match(view, /api\.settings\.save\(\{[\s\S]*\.\.\.companionSettings,[\s\S]*background_agents: payload/)
  assert.match(view, /if \(!saved\) return[\s\S]*setExpandedAuth\(null\)/)
  assert.doesNotMatch(view, /api\.settings\.save\(\{ provider \}\)/)
  assert.match(view, /effort: defaultEffort\(provider\)/)
})

test('appearance indicator waits for the same seeded theme repaint as the palette', () => {
  assert.doesNotMatch(view, /setThemeMode\(newMode\)/)
  assert.match(view, /await themeService\.toggleTheme\(queryClient, currentMode, api\)/)
  assert.match(view, /setThemeMode\(themeModeQuery\.data === 'light'/)
})
