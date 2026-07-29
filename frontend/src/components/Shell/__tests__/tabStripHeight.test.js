// Tab-strip geometry stays identical when a workspace changes leaf count.

import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const shell = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')
const shellCss = readFileSync(new URL('../Shell.css', import.meta.url), 'utf8')
const paneStrip = readFileSync(new URL('../PaneStrip.jsx', import.meta.url), 'utf8')

test('single-pane and tiled tab strips share the pane-model height', () => {
  assert.match(shell, /'--shell-tabstrip-height': `\$\{paneModel\.STRIP_H\}px`/)
  assert.match(shellCss, /height:\s*var\(--shell-tabstrip-height\)/)
  assert.match(paneStrip, /height:\s*STRIP_H/)
  assert.doesNotMatch(
    shellCss,
    /^\s*--shell-tabstrip-height:\s*\d+px/m,
    'the single-pane strip must not introduce a second numeric height source',
  )
})
