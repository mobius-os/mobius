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
const chatViewCss = readFileSync(
  new URL('../ChatView.css', import.meta.url),
  'utf8',
)
const chatViewSource = readFileSync(
  new URL('../ChatView.jsx', import.meta.url),
  'utf8',
)


test('chat context actions follow model selection and continuation policy', () => {
  const picker = composerSource.indexOf('<ChatSettingsPanel')
  const summary = composerSource.indexOf('>Chat summary</span>')
  const inspector = composerSource.indexOf('>What the agent knows</span>')

  assert.ok(picker !== -1 && summary !== -1 && inspector !== -1)
  assert.ok(picker < summary)
  assert.ok(summary < inspector)
  assert.match(settingsSource, /Automatically continue after usage limits/)
  // Restart continuation is always on and has no toggle to render.
  assert.doesNotMatch(settingsSource, /Continue after planned restarts/)
  assert.doesNotMatch(settingsSource, /Chat summar(?:y|ies)/)
})


test('Changes owns contribution attention without adding a composer card', () => {
  assert.match(
    composerSource,
    /useChatChangesOverview\(chatId, initialChangeEntries,[\s\S]*?enabled: Boolean\(!embedded && chatId\)/,
  )
  assert.match(composerSource, /changesOverview\.needsAction \|\| changesOverview\.workState === 'attention'/)
  assert.match(composerSource, /appArtifactAttentionDecision/)
  assert.match(composerSource, /composer-plus__attention-dot/)
  assert.match(composerSource, /composer-popover__row-attention/)
  assert.match(composerSource, /Changes need attention\./)
  assert.match(chatViewSource, /initialChangeEntries=\{chatDiffEntries\}/)
  assert.match(chatViewSource, /appArtifactsReady=\{builtAppsReady && !hidden\}/)
  assert.doesNotMatch(chatViewSource, /ContributionReviewCard|contrib-card-stack/)
})


test('agent context inspector keeps continuity and active turn context visible', () => {
  assert.match(inspectorSource, /title: 'System prompt'/)
  assert.match(inspectorSource, /title: 'Recent chat summaries'/)
  assert.doesNotMatch(inspectorSource, /title: 'Memory/)
  assert.match(inspectorSource, /title: 'Current app context'/)
  assert.match(inspectorSource, /title: 'App report'/)
  assert.match(inspectorSource, /title: 'Compaction handoff'/)
})


test('agent context inspector is centered inside its owning chat', () => {
  const overlayCss = inspectorSource.match(/\.aci__overlay\s*\{([^}]+)\}/)?.[1] || ''
  const chatCss = chatViewCss.match(/\.chat\s*\{([^}]+)\}/)?.[1] || ''

  assert.match(overlayCss, /position:\s*absolute/)
  assert.doesNotMatch(overlayCss, /position:\s*fixed/)
  assert.match(chatCss, /position:\s*relative/)
})
