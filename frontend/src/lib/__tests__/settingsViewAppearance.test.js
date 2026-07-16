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

test('appearance uses explicit native Light and Dark choices', () => {
  assert.match(view, /role="radiogroup"/)
  assert.match(view, /id="settings-appearance-label">Appearance<\/span>/)
  assert.match(view, /type="radio"[\s\S]*value="light"[\s\S]*value="dark"/)
  assert.doesNotMatch(view, /components\/Switch|Toggle dark mode/)
})

test('last model is neutral secondary text without emphasized model markup', () => {
  assert.match(view, /provider-row__status-text settings__last-model/)
  assert.doesNotMatch(view, /settings__last-model-name/)
  assert.match(css, /\.settings__last-model\s*\{[^}]*color:\s*var\(--muted\);[^}]*font-weight:\s*400;/s)
})

test('background-agent rows have one boundary and quiet effort metadata', () => {
  assert.match(view, /className="settings-bg-row__effort">\{effortLabel\} effort/)
  assert.doesNotMatch(view, /className="model-trigger__effort">\{effortLabel\}/)
  assert.match(css, /\.settings-bg-row\s*\{[^}]*border:\s*0;[^}]*background:\s*transparent;/s)
  assert.match(css, /\.settings-bg-row__effort\s*\{[^}]*font-weight:\s*400;/s)
})
