/**
 * Guards that the pre-paint theme IIFE embedded in BOTH index.html and
 * public/app-frame.html is byte-identical to PREPAINT_SRC in
 * src/lib/applyTheme.js — the single source of truth for flash-free first
 * paint. If someone hand-edits one of the inline scripts (or regenerates it
 * differently), this fails so the three copies can't silently drift.
 *
 * Run with:
 *   cd frontend && node --test src/lib/__tests__/applyTheme.prepaint.test.js
 */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, join } from 'node:path'

import { PREPAINT_SRC } from '../applyTheme.js'

const here = dirname(fileURLToPath(import.meta.url))
const frontendRoot = join(here, '..', '..', '..')  // src/lib/__tests__ -> frontend

/**
 * Extract the FIRST inline <script>...</script> in <head> whose body is the
 * pre-paint IIFE (it starts with `(function () {`). Returns the exact inner
 * text (no surrounding tags).
 */
function extractPrepaint(html) {
  // Find the script tag whose content begins (after optional whitespace) with
  // the IIFE opener. The prepaint script is a bare `<script>` (no attrs).
  const re = /<script>(\(function \(\) \{[\s\S]*?\}\)\(\);)<\/script>/
  const m = html.match(re)
  assert.ok(m, 'pre-paint IIFE <script> not found')
  return m[1]
}

test('index.html embeds PREPAINT_SRC verbatim', () => {
  const html = readFileSync(join(frontendRoot, 'index.html'), 'utf8')
  const inline = extractPrepaint(html)
  assert.equal(inline, PREPAINT_SRC,
    'index.html pre-paint script drifted from PREPAINT_SRC')
})

test('app-frame.html embeds PREPAINT_SRC verbatim', () => {
  const html = readFileSync(join(frontendRoot, 'public', 'app-frame.html'), 'utf8')
  const inline = extractPrepaint(html)
  assert.equal(inline, PREPAINT_SRC,
    'app-frame.html pre-paint script drifted from PREPAINT_SRC')
})

test('the two HTML files embed the IDENTICAL pre-paint script', () => {
  const idx = extractPrepaint(readFileSync(join(frontendRoot, 'index.html'), 'utf8'))
  const frame = extractPrepaint(readFileSync(join(frontendRoot, 'public', 'app-frame.html'), 'utf8'))
  assert.equal(idx, frame)
})
