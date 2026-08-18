/* Both provider diff shapes parse into the canonical viewer model. */

import test from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import { toolEditPreview } from '../toolEditPreview.js'
import ToolEditPreview from '../ToolEditPreview.jsx'

test('Codex multi-file patches retain paths, kinds, and line stats', () => {
  const preview = toolEditPreview({
    diff: [
      'diff --git "a//data/café file.js" "b//data/café file.js"',
      '--- "a//data/café file.js"',
      '+++ "b//data/café file.js"',
      '@@ -1 +1 @@',
      '-old',
      '+new',
      'diff --git a/new.js b/new.js',
      'new file mode 100644',
      '--- /dev/null',
      '+++ b/new.js',
      '@@ -0,0 +1 @@',
      '+hello',
    ].join('\n'),
    truncated: false,
  })

  assert.deepEqual(preview.files.map(file => file.path), [
    '/data/café file.js',
    'new.js',
  ])
  assert.deepEqual(preview.files.map(file => file.status), ['M', 'A'])
  assert.deepEqual(
    preview.files.map(file => [file.insertions, file.deletions]),
    [[1, 1], [1, 0]],
  )
})

test('Claude detached edits retain the honest relative-line marker', () => {
  const preview = toolEditPreview({
    diff: [
      'diff --git a/file.js b/file.js',
      '--- a/file.js',
      '+++ b/file.js',
      '@@ -1 +1 @@',
      '-before',
      '+after',
    ].join('\n'),
    relative: true,
    truncated: true,
  })

  assert.equal(preview.relative, true)
  assert.equal(preview.truncated, true)
  assert.equal(preview.files[0].hunks[0].header, 'Changed selection')
  assert.equal(preview.files[0].hunks[0].lines[1].text, 'after')
})

test('missing or unparsable previews remain an ordinary absent detail', () => {
  assert.equal(toolEditPreview(null), null)
  assert.equal(toolEditPreview({ diff: 'not a unified diff' }), null)
})

test('expanded preview renders changed lines and diff metadata', () => {
  const preview = toolEditPreview({
    diff: [
      'diff --git a/app.js b/app.js',
      '--- a/app.js',
      '+++ b/app.js',
      '@@ -1 +1 @@',
      '-before',
      '+after',
    ].join('\n'),
    truncated: true,
  })
  const html = renderToStaticMarkup(createElement(ToolEditPreview, { preview }))

  assert.match(html, />Changes</)
  assert.match(html, />app\.js</)
  assert.match(html, />before</)
  assert.match(html, />after</)
  assert.match(html, />\+1</)
  assert.match(html, /Diff preview truncated\./)
})

test('saved Codex raw add and delete bodies become visible diffs', () => {
  const preview = toolEditPreview({
    diff: [
      'diff --git a/new.py b/new.py',
      'new file mode 100644',
      '--- /dev/null',
      '+++ b/new.py',
      'import os',
      '',
      'print(os.getcwd())',
      'diff --git a/gone.txt b/gone.txt',
      'deleted file mode 100644',
      '--- a/gone.txt',
      '+++ /dev/null',
      'first',
      'second',
    ].join('\n'),
  })

  assert.deepEqual(
    preview.files.map(file => [file.path, file.insertions, file.deletions]),
    [['new.py', 3, 0], ['gone.txt', 0, 2]],
  )
  assert.deepEqual(
    preview.files[0].hunks[0].lines.map(line => [line.type, line.text]),
    [['add', 'import os'], ['add', ''], ['add', 'print(os.getcwd())']],
  )
  assert.deepEqual(
    preview.files[1].hunks[0].lines.map(line => [line.type, line.text]),
    [['del', 'first'], ['del', 'second']],
  )
})

test('a genuinely empty added file remains an honest metadata-only change', () => {
  const preview = toolEditPreview({
    diff: [
      'diff --git a/empty.txt b/empty.txt',
      'new file mode 100644',
      '--- /dev/null',
      '+++ b/empty.txt',
    ].join('\n'),
  })

  assert.equal(preview.files[0].status, 'A')
  assert.equal(preview.files[0].insertions, 0)
  assert.deepEqual(preview.files[0].hunks, [])
})
