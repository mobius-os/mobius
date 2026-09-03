import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = relative => readFileSync(new URL(relative, import.meta.url), 'utf8')

const modal = read('../../components/SettingsView/UpdateReviewModal.jsx')
const modalCss = read('../../components/SettingsView/UpdateReviewModal.css')
const settingsView = read('../../components/SettingsView/SettingsView.jsx')
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

test('apply outcomes close only for explicit clean states and preserve actionable results', () => {
  assert.match(settingsView, /state === 'restart_needed' \|\| state === 'activation_needed' \|\| state === 'up_to_date'/)
  assert.match(settingsView, /state === 'conflict' \|\| state === 'rolled_back'/)
  assert.match(settingsView, /The update returned an unexpected result/)
  assert.match(modal, /result\?\.state === 'conflict' \|\| result\?\.state === 'rolled_back'/)
  assert.match(modal, /result\.state === 'activation_needed'/)
  assert.match(modal, /result\.state === 'up_to_date'/)
  assert.doesNotMatch(modal, /if \(result\?\.ok\) onClose\(\)/)
  assert.match(modal, /applyProgress\?\.plan_id === preview\?\.plan_id/)
})

test('Railway image reviews rebuild the exact immutable image instead of applying in place', () => {
  assert.match(modal, /reviewedUpdateUsesContainerRebuild\(preview\)/)
  assert.match(modal, /rebuildUpdate \? onRebuild\(plan\) : onApply\(plan\)/)
  assert.match(modal, /image_digest: preview\?\.image_digest/)
  assert.match(modal, /!rebuildUpdate \|\| preview\?\.image_digest/)
  assert.match(modal, /Rebuild to update/)
  assert.match(modal, /Starting the exact reviewed official image…/)
  assert.match(settingsView, /api\.platform\.rebuild\(plan\)/)
  assert.match(settingsView, /\{ reviewedUpdate: true \}/)
  assert.match(settingsView, /\|\| rebuildIsActive\(rebuildStatus\)/)
  assert.match(settingsView, /rebuildRequestOutcome\(body, \{ reviewedUpdate \}\)/)
  assert.match(settingsView, /rebuildStatus\?\.error[\s\S]*rebuildStatus\?\.message/)
})

test('an exact no-change completes a reviewed rebuild while failures remain visible', () => {
  assert.match(modal, /'queued', 'preparing', 'replacing', 'verifying', 'succeeded',[\s\S]*'no_change'/)
  assert.match(settingsView, /ok: outcome\.accepted/)
  assert.match(settingsView, /if \(outcome\.alreadyCurrent\) \{[\s\S]*await refreshPlatform\(\)/)
  assert.doesNotMatch(settingsView, /reviewed image is already running, but this update is still pending/)
  assert.match(settingsView, /rebuildReviewedUpdateRef\.current = false[\s\S]*return \{ ok: false, message \}/)
})

test('the apply response is a truthful fallback when status refresh fails', () => {
  assert.match(updateState, /function platformStatusFromApply\(previous, result\)/)
  assert.match(updateState, /available: state === 'rolled_back'/)
  assert.match(updateState, /conflict_paths: Array\.isArray\(result\.conflict_paths\)/)
  assert.match(updateState, /conflict_chat_id: state === 'conflict' \? \(result\.chat_id \|\| null\) : null/)
  assert.match(
    settingsView,
    /setPlatform\(current => platformStatusFromApply\(current, body\)\)[\s\S]*await refreshPlatform\(\{ preserveCurrentOnFailure: true \}\)/,
  )
  assert.match(
    settingsView,
    /if \(!preserveCurrentOnFailure\) \{[\s\S]*setPlatform\(current => platformStatusUnavailable\(current\)\)/,
  )
})

test('apply errors have exactly one live alert owner', () => {
  assert.match(modal, /<div className="urm__error">[\s\S]*<Alert color="danger"/)
  assert.doesNotMatch(modal, /className="urm__error" role="alert"/)
})

test('result and close focus always land on live tabbable controls', () => {
  assert.match(modal, /ref=\{resultActionRef\}/)
  assert.match(modal, /tabIndex=\{-1\}/)
  assert.doesNotMatch(modal, /resultHeadingRef|tabIndex=\{blocked/)
  assert.match(settingsView, /ref=\{platformActionRef\}/)
  assert.match(settingsView, /restorePlatformActionFocusRef\.current = true/)
  assert.match(
    settingsView,
    /if \([\s\S]*reviewOpen[\s\S]*platformPhase !== 'idle'[\s\S]*requestAnimationFrame\(\(\) => \{[\s\S]*platformActionRef\.current/,
  )
})

test('a newly discovered update inherits focus from the replaced check action', () => {
  assert.match(settingsView, /freshPlatform = await res\.json\(\)/)
  assert.match(
    settingsView,
    /if \(freshPlatform\?\.available\) \{[\s\S]*requestAnimationFrame\(\(\) => \{[\s\S]*platformActionRef\.current\?\.focus/,
  )
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
