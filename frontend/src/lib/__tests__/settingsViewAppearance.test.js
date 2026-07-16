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

test('appearance is one full-row icon switch', () => {
  assert.match(view, /settings__section--appearance/)
  assert.match(view, /role="switch"/)
  assert.match(view, /onClick=\{toggleTheme\}/)
  assert.match(view, /<Sun[\s\S]*<Moon/)
  assert.doesNotMatch(view, /<span>Light<\/span>|<span>Dark<\/span>|type="radio"/)
  assert.match(css, /\.settings__section--appearance\s*\{[^}]*padding:\s*0;/s)
  assert.match(css, /\.settings__appearance\s*\{[^}]*width:\s*100%;/s)
})

test('model and synced commit use the same normal-weight standard highlight', () => {
  assert.match(view, /provider-row__status-text settings__last-model/)
  assert.match(view, /Last model: <span className="settings__standard-highlight">/)
  assert.match(view, /Synced to [\s\S]*settings__standard-highlight/)
  assert.doesNotMatch(view, /Serving local \{mobiusVersion\.localSha\}/)
  assert.match(css, /\.settings__last-model\s*\{[^}]*color:\s*var\(--muted\);[^}]*font-weight:\s*400;/s)
  assert.match(css, /\.settings__standard-highlight\s*\{[^}]*color:\s*var\(--green\);[^}]*font-weight:\s*inherit;/s)
})

test('background agents are always draggable without reorder chrome or a trailing caret', () => {
  assert.match(view, /className="settings-bg-row__effort">\{effortLabel\} effort/)
  assert.match(view, /reorderMode\s*\n/)
  assert.match(view, /<GripVertical size=\{18\}/)
  assert.doesNotMatch(view, /settings-agent-group__reorder|>Reorder<|model-trigger__caret/)
  assert.match(view, /Tried in order when quota or authentication fails\./)
  assert.match(css, /\.settings-bg-row\s*\{[^}]*border:\s*0;[^}]*background:\s*transparent;/s)
  assert.match(css, /\.settings-bg-row__effort\s*\{[^}]*font-weight:\s*400;/s)
  assert.doesNotMatch(view, /dropPosition|settings-bg-row--drop-before|settings-bg-row--drop-after/)
  assert.doesNotMatch(css, /settings-bg-row--drop-before|settings-bg-row--drop-after/)
})

test('appearance indicator waits for the same seeded theme repaint as the palette', () => {
  assert.doesNotMatch(view, /setThemeMode\(newMode\)/)
  assert.match(view, /await themeService\.toggleTheme\(queryClient, currentMode, api\)/)
  assert.match(view, /setThemeMode\(themeModeQuery\.data === 'light'/)
})
