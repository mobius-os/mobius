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
  assert.match(settingsSource, /Automatically continue<br \/>after usage limits/)
  // Restart continuation is always on and has no toggle to render.
  assert.doesNotMatch(settingsSource, /Continue after planned restarts/)
  assert.doesNotMatch(settingsSource, /Chat summar(?:y|ies)/)
})


test('the Brain surfaces only work that needs the owner, never a not-upstream-yet nag', () => {
  assert.match(
    composerSource,
    /useChatChangesOverview\(chatId, initialChangeEntries,[\s\S]*?enabled: Boolean\(!embedded && chatReady && chatId\)/,
  )
  assert.doesNotMatch(composerSource, /hasPendingUpstreamWork|pendingUpstreamWork/)
  assert.doesNotMatch(composerSource, /composer-plus__upstream-warning|composer-plus__attention-diamond/)
  assert.doesNotMatch(composerSource, /TriangleExclamationErrorWarning/)
  assert.doesNotMatch(composerSource, /not upstream yet/i)
  assert.match(composerSource, /composer-plus__activity-dot/)
  assert.match(composerSource, /changesNeedOwner && \([\s\S]*?composer-popover__row-attention[\s\S]*?Needs you/)
  assert.doesNotMatch(composerSource, /composer-plus__attention-dot/)
  assert.match(chatViewSource, /initialChangeEntries=\{chatDiffEntries\}/)
  assert.doesNotMatch(chatViewSource, /ContributionReviewCard|contrib-card-stack/)
})


test('agent context inspector keeps continuity and active turn context visible', () => {
  assert.match(inspectorSource, /title: 'System prompt'/)
  assert.match(inspectorSource, /title: 'Recent chat summaries'/)
  assert.match(inspectorSource, /Names and chat summary excerpts from your latest conversations/)
  assert.doesNotMatch(inspectorSource, /title: 'Memory/)
  assert.match(inspectorSource, /title: 'Current app context'/)
  assert.match(inspectorSource, /title: 'App report'/)
  assert.match(inspectorSource, /title: 'Compaction handoff'/)
})


test('chat continuity labels describe existing layers without swapping their data', () => {
  const viewer = readFileSync(new URL('../ChatSummaryViewer.jsx', import.meta.url), 'utf8')

  assert.match(viewer, /<h3>Chat name<\/h3>/)
  assert.match(viewer, /<h3>Chat summary<\/h3>/)
  assert.match(viewer, /<h3>Full digest<\/h3>/)
  assert.doesNotMatch(viewer, /<h3>Digest<\/h3>|<h3>Full summary<\/h3>/)
  assert.match(viewer, /digest: data\.chat_digest \|\| ''/)
  assert.match(viewer, /summary: data\.chat_summary \|\| ''/)
  assert.match(viewer, /whole conversation in brief, with connected recent progress/)
  assert.match(composerSource, /Name, summary, full digest/)
})


test('agent context inspector is centered inside its owning chat', () => {
  const overlayCss = inspectorSource.match(/\.aci__overlay\s*\{([^}]+)\}/)?.[1] || ''
  const chatCss = chatViewCss.match(/\.chat\s*\{([^}]+)\}/)?.[1] || ''

  assert.match(overlayCss, /position:\s*absolute/)
  assert.doesNotMatch(overlayCss, /position:\s*fixed/)
  assert.match(chatCss, /position:\s*relative/)
})
