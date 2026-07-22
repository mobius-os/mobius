import { readFileSync } from 'node:fs'
import assert from 'node:assert/strict'
import { test } from 'node:test'


const composerSource = readFileSync(
  new URL('../ComposerPopover.jsx', import.meta.url),
  'utf8',
)
const settingsSource = readFileSync(
  new URL('../ChatSettingsPanel.jsx', import.meta.url),
  'utf8',
)
const inspectorSource = readFileSync(
  new URL('../AgentContextInspector.jsx', import.meta.url),
  'utf8',
)


test('chat context actions follow model selection and continuation policy', () => {
  const picker = composerSource.indexOf('<ChatSettingsPanel')
  const summary = composerSource.indexOf('>Chat summary</span>')
  const inspector = composerSource.indexOf('>What the agent knows</span>')

  assert.ok(picker !== -1 && summary !== -1 && inspector !== -1)
  assert.ok(picker < summary)
  assert.ok(summary < inspector)
  assert.match(settingsSource, /Continue after limits and restarts/)
  assert.doesNotMatch(settingsSource, /Chat summar(?:y|ies)/)
})


test('agent context inspector keeps continuity and active turn context visible', () => {
  assert.match(inspectorSource, /title: 'System prompt'/)
  assert.match(inspectorSource, /title: 'Recent chat summaries'/)
  assert.doesNotMatch(inspectorSource, /title: 'Memory/)
  assert.match(inspectorSource, /title: 'Current app context'/)
  assert.match(inspectorSource, /title: 'App report'/)
  assert.match(inspectorSource, /title: 'Compaction handoff'/)
})
