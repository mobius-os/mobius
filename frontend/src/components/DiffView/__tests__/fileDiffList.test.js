import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

import FileDiffList, {
  bidiSafeDirectory,
  buildFileRows,
  collapseFileRows,
  filePreviewState,
  splitPath,
} from '../FileDiffList.jsx'
import DiffView from '../DiffView.jsx'
import {
  DIFF_VIEWER_STYLES,
  ensureDiffViewerStyles,
} from '../styles.js'

const canonicalFolder = new URL('../', import.meta.url)

test('canonical sources are flat, self-contained, and have no CSS imports', () => {
  const sourceNames = readdirSync(canonicalFolder)
    .filter((name) => /\.(?:js|jsx)$/.test(name))
    .sort()
  assert.deepEqual(sourceNames, [
    'DiffView.jsx',
    'FileDiffList.jsx',
    'parseUnifiedDiff.js',
    'styles.js',
  ])

  for (const name of sourceNames) {
    const source = readFileSync(new URL(name, canonicalFolder), 'utf8')
    assert.match(source, /CANONICAL DIFF VIEWER: copy this entire folder verbatim/)
    assert.doesNotMatch(
      source,
      /\b(?:import|export)\b[^'"\n]*['"][^'"]+\.css['"]/,
      `${name} must not create an esbuild CSS side-output`,
    )
    const imports = [...source.matchAll(
      /^\s*import(?:\s+[\s\S]+?\s+from)?\s*['"]([^'"]+)['"]/gm,
    )]
      .map((match) => match[1])
    assert.ok(
      imports.every((specifier) => specifier === 'react' || specifier.startsWith('./')),
      `${name} may import only React and flat siblings: ${imports.join(', ')}`,
    )
    assert.ok(
      imports.filter((specifier) => specifier.startsWith('./'))
        .every((specifier) => !specifier.slice(2).includes('/')),
      `${name} has a non-flat sibling import`,
    )
  }
})

test('splitPath and bidi anchoring preserve leading-neutral directories', () => {
  assert.deepEqual(splitPath('README.md'), { dir: '', base: 'README.md' })
  assert.deepEqual(splitPath('.github/workflows/ci.yml'), {
    dir: '.github/workflows',
    base: 'ci.yml',
  })
  assert.deepEqual(splitPath('/etc/deep/path/hosts.conf'), {
    dir: '/etc/deep/path',
    base: 'hosts.conf',
  })
})

test('leading punctuation is LRM-anchored and the slash stays out of the RTL span', () => {
  // Both halves are load-bearing and were verified by render: without the LRM,
  // ".github/workflows" renders as "github/workflows."; with the slash INSIDE
  // the RTL span it is reordered to the visual start ("/srcApp.jsx").
  assert.equal(bidiSafeDirectory('.github/workflows'), '\u200E.github/workflows')
  assert.equal(bidiSafeDirectory(''), '')
  const html = renderToStaticMarkup(
    createElement(FileDiffList, {
      files: [],
      summaryOverrides: [{ path: 'src/App.jsx', insertions: 1, deletions: 0 }],
    }),
  )
  assert.match(html, /file-diff-list__separator[^>]*>\/</,
    'the separator is its own span, outside the RTL truncation span')
  assert.doesNotMatch(html, /file-diff-list__dir[^>]*>[^<]*\/</,
    'the directory span must not carry a trailing slash')
})

test('row building reserves exact paths before using rename/copy aliases', () => {
  const rename = {
    path: 'new.txt',
    oldPath: 'old.txt',
    newPath: 'new.txt',
    status: 'R',
    hunks: [{ header: 'rename body', lines: [] }],
  }
  const exactOld = {
    path: 'old.txt',
    oldPath: 'old.txt',
    newPath: 'old.txt',
    status: 'M',
    hunks: [{ header: 'exact body', lines: [] }],
  }
  const rows = buildFileRows([rename, exactOld], [
    { path: 'old.txt', status: 'M', insertions: 40, deletions: 12 },
    { path: 'new.txt', status: 'R', insertions: 1, deletions: 1 },
  ])

  assert.equal(rows[0].file, exactOld)
  assert.equal(rows[1].file, rename)
  assert.equal(rows[0].insertions, 40)
  assert.equal(rows[0].deletions, 12)

  const [aliasFallback] = buildFileRows([rename], [
    { path: 'old.txt', status: 'R' },
  ])
  assert.equal(aliasFallback.file, rename)
})

test('preview state distinguishes unavailable bodies from real empty entries', () => {
  const unavailable = buildFileRows([], [
    { path: 'timed-out.txt', status: 'M', insertions: 40, deletions: 12 },
  ])[0]
  const empty = { file: { binary: false, hunks: [] } }
  const body = { file: { binary: false, hunks: [{ header: '@@', lines: [] }] } }

  assert.equal(filePreviewState(unavailable, false, false), 'unavailable')
  assert.equal(filePreviewState(unavailable, true, false), 'truncated')
  assert.equal(filePreviewState(empty, false, false), 'empty')
  assert.equal(filePreviewState(empty, true, true), 'truncated')
  assert.equal(filePreviewState(body, false, false), 'diff')
})

test('collapse decision keeps eight rows and samples six above the threshold', () => {
  const eight = Array.from({ length: 8 }, (_, index) => index)
  const nine = Array.from({ length: 9 }, (_, index) => index)
  assert.deepEqual(collapseFileRows(eight), {
    collapsed: false,
    visibleRows: eight,
  })
  assert.equal(collapseFileRows(nine).collapsed, true)
  assert.deepEqual(collapseFileRows(nine).visibleRows, nine.slice(0, 6))
  assert.deepEqual(collapseFileRows(nine, true), {
    collapsed: false,
    visibleRows: nine,
  })
})

test('collapsed rows expose valid disclosure and announced stat semantics', () => {
  const html = renderToStaticMarkup(
    createElement(FileDiffList, {
      files: [],
      summaryOverrides: [
        { path: '.github/workflows/ci.yml', status: 'C', insertions: 12, deletions: 3 },
        { path: 'script.sh', status: 'T', insertions: 0, deletions: 0 },
      ],
    }),
  )
  assert.match(html, /aria-expanded="false"/)
  assert.doesNotMatch(html, /aria-controls=/)
  assert.match(html, /role="img" aria-label="12 additions, 3 deletions"/)
  assert.match(html, />copied</)
  assert.match(html, />type changed</)
})

test('DiffView reserves the empty message for a parsed entry with no hunks', () => {
  assert.match(
    renderToStaticMarkup(createElement(DiffView, {
      file: { path: 'empty.txt', hunks: [] },
    })),
    /No textual changes to preview\./,
  )
  assert.doesNotMatch(
    renderToStaticMarkup(createElement(FileDiffList, {
      files: [],
      summaryOverrides: [{ path: 'missing.txt', insertions: 40, deletions: 12 }],
    })),
    /No textual changes to preview\./,
  )
})

test('canonical styles fill wide scroll content and protect narrow row stats', () => {
  assert.match(
    DIFF_VIEWER_STYLES,
    /\.diff-view\s*\{[^}]*width: max-content;[^}]*min-width: 100%;/,
  )
  assert.match(
    DIFF_VIEWER_STYLES,
    /\.file-diff-list__basename\s*\{[^}]*min-width: 0;[^}]*overflow: hidden;[^}]*text-overflow: ellipsis;/,
  )
  assert.doesNotMatch(DIFF_VIEWER_STYLES, /\.file-diff-list__panel \.diff-view/)
  assert.match(DIFF_VIEWER_STYLES, /var\(--green, #16a34a\)/)
  assert.match(DIFF_VIEWER_STYLES, /var\(--danger, #ef4444\)/)
})

test('style injection is SSR-safe and idempotent', () => {
  const originalDocument = globalThis.document
  const elements = new Map()
  const appended = []
  globalThis.document = {
    head: {
      appendChild(element) {
        elements.set(element.id, element)
        appended.push(element)
      },
    },
    documentElement: null,
    createElement(tagName) {
      return { tagName, id: '', textContent: '' }
    },
    getElementById(id) {
      return elements.get(id) || null
    },
  }

  try {
    const first = ensureDiffViewerStyles()
    const second = ensureDiffViewerStyles()
    assert.equal(first, second)
    assert.equal(appended.length, 1)
    assert.equal(first.tagName, 'style')
    assert.equal(first.textContent, DIFF_VIEWER_STYLES)
  } finally {
    if (originalDocument === undefined) delete globalThis.document
    else globalThis.document = originalDocument
  }
})
