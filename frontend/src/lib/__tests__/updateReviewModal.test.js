import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = relative => readFileSync(new URL(relative, import.meta.url), 'utf8')

const modal = read('../../components/SettingsView/UpdateReviewModal.jsx')
const modalCss = read('../../components/SettingsView/UpdateReviewModal.css')
const diffView = read('../../components/DiffView/DiffView.jsx')
const diffCss = read('../../components/DiffView/DiffView.css')

test('platform update files own independent accessible disclosures', () => {
  assert.match(modal, /useState\(\(\) => new Set\(\)\)/)
  assert.match(modal, /setOpenFiles\(\(current\) =>/)
  assert.match(modal, /className="urm__file-toggle"[\s\S]*aria-expanded=\{isOpen\}/)
  assert.match(modal, /<DiffView file=\{diffEntry\} \/>/)
  assert.match(modalCss, /\.urm__file-toggle:focus-visible/)
})

test('the combined raw-diff toggle is gone and truncation is explained per file', () => {
  assert.doesNotMatch(modal, /diffOpen|Show changes|Hide changes|<pre/)
  assert.doesNotMatch(modalCss, /\.urm__diff(?:\s|\{|--)/)
  assert.match(modal, /parseUnifiedDiff\(preview\?\.diff\)/)
  assert.match(modal, /diffFileByPath\(parsedDiff\)/)
  assert.match(modal, /preview\?\.diff_truncated/)
  assert.match(
    modal,
    /Diff not shown — this update is large; the full change applies on Apply\./,
  )
})

test('DiffView stays generic, semantic, and keyboard-scrollable', () => {
  assert.doesNotMatch(diffView, /platformUpdatePreview|UpdateReviewModal|api\./)
  assert.match(diffView, /if \(!file\) return null/)
  assert.match(diffView, /Binary file — no preview/)
  assert.match(diffView, /tabIndex=\{0\}/)
  assert.match(diffView, /diff-view__line--\$\{line\.type\}/)
  assert.match(diffCss, /overflow-x: auto/)
  assert.match(diffCss, /var\(--green\)/)
  assert.match(diffCss, /var\(--danger\)/)
})
