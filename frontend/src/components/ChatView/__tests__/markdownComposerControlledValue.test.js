import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const editorSrc = readFileSync(new URL('../MarkdownComposer.jsx', import.meta.url), 'utf8')
const valueEffect = editorSrc.match(
  /useLayoutEffect\(\(\) => \{\n    const view = viewRef\.current\n    if \(!view \|\| view\.state\.doc\.toString\(\) === value\) return[\s\S]*?\n  \}, \[value\]\)/,
)?.[0] || ''

test('a controlled value reaches the editor as a transaction Android typing can follow', () => {
  assert.ok(valueEffect, 'the controlled-value effect must exist')
  // Android Chrome types through an EditContext that only transactions keep
  // in sync. After setState it still holds the previous message, so keys
  // typed after a send are dropped until the box is refocused.
  assert.doesNotMatch(editorSrc, /\.setState\(/,
    'replacing the editor state desynchronizes Android keyboards')
  assert.match(valueEffect, /changes: \{ from: 0, to: view\.state\.doc\.length, insert: value \}/)
})

test('a sent message can never come back through undo', () => {
  assert.match(editorSrc, /historyRef\.current\.of\(history\(\)\)/,
    'undo history must be reconfigurable')
  const off = valueEffect.indexOf('historyRef.current.reconfigure([])')
  const on = valueEffect.indexOf('historyRef.current.reconfigure(history())')
  assert.ok(off > 0 && on > off,
    'the replacement lands with history off, then a fresh history starts')
})

test('a controlled value is not reported back as typing', () => {
  assert.equal((valueEffect.match(/annotations: controlledValue\.of\(true\)/g) || []).length, 2)
  assert.match(editorSrc,
    /transactions\.some\(transaction => transaction\.docChanged && !transaction\.annotation\(controlledValue\)\)/,
    'history browsing and dictation must not see a send or recall as an edit')
})
