import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = relative => readFileSync(new URL(relative, import.meta.url), 'utf8')

const modal = read('../../components/SettingsView/UpdateReviewModal.jsx')
const modalCss = read('../../components/SettingsView/UpdateReviewModal.css')
const settingsView = read('../../components/SettingsView/SettingsView.jsx')
const updates = read('../../components/SettingsView/PlatformUpdates.jsx')
const requests = read('../../components/SettingsView/usePlatformUpdates.js')
const updateState = read('../platformUpdateState.js')
const diffView = read('../../components/DiffView/DiffView.jsx')
const diffStyles = read('../../components/DiffView/styles.js')

test('platform update delegates file disclosures to the canonical list', () => {
  assert.match(modal, /import UnifiedDiff from '\.\.\/DiffView\/UnifiedDiff\.jsx'/)
  assert.match(modal, /<UnifiedDiff[\s\S]*diff=\{preview\?\.diff\}/)
  assert.match(modal, /summaryOverrides=\{files\}/)
  assert.match(modal, /diffTruncated=\{!!preview\?\.diff_truncated\}/)
  assert.doesNotMatch(modal, /urm__file|toggleFile|diffByPath|<DiffView/)
  assert.doesNotMatch(modalCss, /\.urm__file/)
})

test('the combined raw-diff toggle is gone and truncation is explained per file', () => {
  assert.doesNotMatch(modal, /diffOpen|Show changes|Hide changes|<pre/)
  assert.doesNotMatch(modalCss, /\.urm__diff(?:\s|\{|--)/)
  assert.match(modal, /preview\?\.diff_truncated/)
})

test('Settings delegates update lifecycle and presentation to one owner', () => {
  assert.match(settingsView, /<PlatformUpdates/)
  assert.doesNotMatch(settingsView, /api\.platform\.(apply|rebuild)|api\.admin\.restart/)
  assert.match(updates, /usePlatformUpdates/)
  assert.match(updates, /<UpdateReviewModal/)
  assert.match(requests, /rebuildRequestOutcome/)
  assert.match(requests, /platformStatusFromApply/)
})

test('immutable review drives both source apply and container replacement', () => {
  assert.match(modal, /reviewedUpdateUsesContainerRebuild\(preview\)/)
  for (const field of ['plan_id', 'current_sha', 'target_sha', 'image_digest']) {
    assert.match(modal, new RegExp(`${field}: preview\\.${field}`))
  }
  assert.match(modal, /onRebuild\(plan\) : onApply\(plan\)/)
  assert.match(requests, /api\.platform\.rebuild\(plan\)/)
  assert.match(requests, /api\.platform\.apply\(plan\)/)
  assert.match(requests, /reviewedUpdate: true/)
})

test('unfinished activation can be reviewed independently of incoming source', () => {
  assert.match(modal, /preview\?\.actionable/)
  assert.match(modal, /preview\?\.operation === 'finish'/)
  assert.match(modal, /updatePreview\(\{ intent \}\)/)
  assert.match(updates, /openReview\('finish'\)/)
  assert.doesNotMatch(modal, /disabled=\{[^}]*!preview\?\.available/)
})

test('a definitive rebuild failure refreshes status without erasing the reviewed state', () => {
  assert.match(
    requests,
    /setErrorCode\(cause\.code \|\| ''\)[\s\S]*refreshPlatform\(\{ preserveCurrentOnFailure: true \}\)/,
  )
})

test('successful apply projection survives an unavailable follow-up status read', () => {
  assert.match(updateState, /function platformStatusFromApply\(previous, result\)/)
  assert.match(updateState, /available: state === 'rolled_back'/)
  assert.match(requests, /setPlatform\(current => platformStatusFromApply\(current, body\)\)/)
  assert.match(requests, /refreshPlatform\(\{ preserveCurrentOnFailure: true \}\)/)
  assert.match(requests, /if \(!preserveCurrentOnFailure\) setPlatform/)
})

test('errors have one alert owner and results focus a live control', () => {
  assert.match(modal, /<div className="urm__error">/ )
  assert.match(modal, /<Alert color="danger"/)
  assert.match(modal, /buttonRef=\{resultActionRef\}/)
  assert.doesNotMatch(modal, /className="urm__error" role="alert"/)
  assert.match(modal, /ref=\{resultActionRef\}/)
  assert.match(modal, /tabIndex=\{-1\}/)
  assert.match(updates, /ref=\{actionRef\}/)
  assert.match(updates, /restoreFocus\.current = true/)
  assert.match(updates, /actionRef\.current\.focus/)
})

test('Settings keeps current update feedback and reserves technical detail for review', () => {
  assert.doesNotMatch(updates, /Last container update|terminalRebuild|platform-updates__details/)
  assert.match(updates, /rebuildProgressMessage\(rebuild\)/)
  assert.match(updates, /update\.error && <Alert/)
  assert.doesNotMatch(updates, /rebuildRequested|rebuildReviewedUpdateRef/)
  assert.match(modal, /<details[^>]*urm__technical/)
  assert.match(modal, /<summary>Technical details/)
})

test('DiffView stays generic, semantic, and keyboard-scrollable', () => {
  assert.doesNotMatch(diffView, /platformUpdatePreview|UpdateReviewModal|api\./)
  assert.doesNotMatch(diffView, /dangerouslySetInnerHTML/)
  assert.match(diffView, /if \(!file\) return null/)
  assert.match(diffView, /Binary file — no preview/)
  assert.match(diffView, /No textual changes to preview\./)
  assert.match(diffView, /tabIndex=\{0\}/)
  assert.match(diffView, /diff-view__line--\$\{line\.type\}/)
  assert.match(diffStyles, /white-space: pre-wrap/)
  assert.match(diffStyles, /overflow-x: hidden/)
  assert.doesNotMatch(diffStyles, /width: max-content/)
  assert.match(diffStyles, /var\(--green, #16a34a\)/)
  assert.match(diffStyles, /var\(--danger, #ef4444\)/)
})


test('update repair reuses the shared lifecycle and keeps mobile failure text below actions', () => {
  const repair = read('../../components/SettingsView/UpdateRepairAction.jsx')
  assert.match(repair, /useAgentRepair/)
  assert.doesNotMatch(repair, /api\.chats|fetch\(|window\.location/)
  assert.match(modalCss, /\.urm__foot \{[^}]*flex-wrap: wrap/)
  assert.match(modalCss, /\.urm__foot \.platform-updates__description \{[^}]*flex-basis: 100%/)
})


test('update review uses compact Settings controls without stretching mobile buttons', () => {
  assert.match(modal, /settings__btn settings__btn--sm settings__btn--outline/)
  assert.match(modal, /className="settings__btn settings__btn--sm"/)
  assert.doesNotMatch(modalCss, /\.urm__btn|flex: 1(?:;|\s)/)
})

test('asking for help names one ordinary chat and explains the restart boundary', () => {
  const repair = read('../../components/SettingsView/UpdateRepairAction.jsx')
  assert.match(repair, /'Ask Möbius'/)
  assert.match(modal, /Open a chat with the update details included/)
  assert.match(modal, /asking before any restart/)
  assert.doesNotMatch(modal + repair, /repair chat|recovery chat|help prepare it/)
})
