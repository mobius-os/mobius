import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = relative => readFileSync(new URL(relative, import.meta.url), 'utf8')

const modal = read('../../components/SettingsView/UpdateReviewModal.jsx')
const modalCss = read('../../components/SettingsView/UpdateReviewModal.css')
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
