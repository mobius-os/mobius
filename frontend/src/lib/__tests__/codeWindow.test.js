import { test } from 'node:test'
import assert from 'node:assert/strict'
import { lineNumbersFor, windowedCode } from '../codeWindow.js'

test('windowedCode passes a small file through unwindowed with its real line count', () => {
  assert.deepEqual(
    windowedCode('a\nb\nc', 100),
    { text: 'a\nb\nc', totalLines: 3, shownLines: 3, windowed: false },
  )
})

test('windowedCode cuts a large file on a line boundary and reports both counts', () => {
  const content = Array.from({ length: 50 }, (_, i) => `line-${i + 1}`).join('\n')
  const result = windowedCode(content, 100)
  assert.equal(result.windowed, true)
  assert.equal(result.totalLines, 50)
  assert.ok(result.shownLines < 50)
  // Every kept line is intact — the window never ends mid-line.
  const lines = result.text.split('\n')
  assert.equal(lines[lines.length - 1], `line-${lines.length}`)
  assert.equal(result.shownLines, lines.length)
})

test('windowedCode falls back to the raw cap for one enormous line', () => {
  const result = windowedCode('x'.repeat(500), 100)
  assert.equal(result.windowed, true)
  assert.equal(result.text.length, 100)
  assert.equal(result.totalLines, 1)
  assert.equal(result.shownLines, 1)
})

test('windowedCode treats empty content as zero lines', () => {
  assert.deepEqual(
    windowedCode('', 10),
    { text: '', totalLines: 0, shownLines: 0, windowed: false },
  )
})

test('lineNumbersFor renders one number per line with no trailing newline', () => {
  assert.equal(lineNumbersFor(3), '1\n2\n3')
  assert.equal(lineNumbersFor(0), '')
})
