import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = relative => readFileSync(new URL(relative, import.meta.url), 'utf8')

const modal = read('../../components/SettingsView/UpdateReviewModal.jsx')
const modalCss = read('../../components/SettingsView/UpdateReviewModal.css')
const settingsView = read('../../components/SettingsView/SettingsView.jsx')
const diffView = read('../../components/DiffView/DiffView.jsx')
const diffStyles = read('../../components/DiffView/styles.js')

test('platform update delegates file disclosures to the canonical list', () => {
  assert.match(modal, /import FileDiffList from '\.\.\/DiffView\/FileDiffList\.jsx'/)
  assert.match(modal, /import \{ parseUnifiedDiff \} from '\.\.\/DiffView\/parseUnifiedDiff\.js'/)
  assert.match(modal, /parseUnifiedDiff\(preview\?\.diff\)/)
  assert.match(modal, /<FileDiffList[\s\S]*files=\{parsedFiles\}/)
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
  assert.match(settingsView, /state === 'restart_needed' \|\| state === 'up_to_date'/)
  assert.match(settingsView, /state === 'conflict' \|\| state === 'rolled_back'/)
  assert.match(settingsView, /The update returned an unexpected result/)
  assert.match(modal, /result\?\.state === 'conflict' \|\| result\?\.state === 'rolled_back'/)
  assert.match(modal, /result\.state === 'restart_needed' \|\| result\.state === 'up_to_date'/)
  assert.doesNotMatch(modal, /if \(result\?\.ok\) onClose\(\)/)
  assert.match(modal, /applyProgress\?\.plan_id === preview\?\.plan_id/)
})

test('the apply response is a truthful fallback when status refresh fails', () => {
  assert.match(settingsView, /function platformStatusFromApply\(previous, result\)/)
  assert.match(settingsView, /available: state === 'rolled_back'/)
  assert.match(settingsView, /conflict_paths: Array\.isArray\(result\.conflict_paths\)/)
  assert.match(settingsView, /conflict_chat_id: state === 'conflict' \? \(result\.chat_id \|\| null\) : null/)
  assert.match(
    settingsView,
    /setPlatform\(current => platformStatusFromApply\(current, body\)\)[\s\S]*await refreshPlatform\(\)/,
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
  assert.match(settingsView, /requestAnimationFrame\(\(\) => \{[\s\S]*platformActionRef\.current\?\.focus/)
})

test('DiffView stays generic, semantic, and keyboard-scrollable', () => {
  assert.doesNotMatch(diffView, /platformUpdatePreview|UpdateReviewModal|api\./)
  assert.doesNotMatch(diffView, /dangerouslySetInnerHTML/)
  assert.match(diffView, /if \(!file\) return null/)
  assert.match(diffView, /Binary file — no preview/)
  assert.match(diffView, /No textual changes to preview\./)
  assert.match(diffView, /tabIndex=\{0\}/)
  assert.match(diffView, /diff-view__line--\$\{line\.type\}/)
  assert.match(diffStyles, /width: max-content/)
  assert.match(diffStyles, /var\(--green, #16a34a\)/)
  assert.match(diffStyles, /var\(--danger, #ef4444\)/)
})
