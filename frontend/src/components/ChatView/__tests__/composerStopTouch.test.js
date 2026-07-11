import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const src = readFileSync(new URL('../ChatInputBar.jsx', import.meta.url), 'utf8')
const stopBlock = src.match(/key="stop"[\s\S]*?aria-label="Stop"/)?.[0] || ''

test('Stop dispatches on touchend and preserves composer focus on pointerdown', () => {
  assert.match(stopBlock, /onPointerDown=\{\(e\) => e\.preventDefault\(\)\}/,
    'Stop must not let a touch pointerdown blur the textarea before the action')
  assert.match(stopBlock, /onTouchEnd=\{\(e\) => \{ e\.preventDefault\(\); onStop\(\) \}\}/,
    'Stop should fire on touchend instead of relying on a synthesized click')
  assert.match(stopBlock, /onClick=\{onStop\}/,
    'Desktop click must keep working')
})
