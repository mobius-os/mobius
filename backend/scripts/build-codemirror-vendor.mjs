// Build a self-hosted CodeMirror 6 ESM bundle for the mini-app import map.
//
// Why this exists: mini-apps get a live-inline markdown editor from
// CodeMirror 6. Served from esm.sh, CM6 is a multi-module graph whose
// `@codemirror/state` MUST be a singleton (it does `instanceof` checks across
// the view/language/commands boundary); two independently-bundled entries each
// inline their own `state` and break at runtime. An offline app could also miss
// an uncached sub-module. We bundle the exact pieces the editor uses into ONE
// `codemirror.mjs` (state included exactly once -> singleton guaranteed) served
// same-origin under /vendor, exactly as three.js and React are. One bundled URL
// is one warm-cache fetch (CacheFirst /vendor route), so offline is
// deterministic - the same rationale as build-react-vendor.mjs.
//
// CM6 ships as real ESM (unlike react/react-dom's CommonJS), so a single
// no-externals bundle is enough - no facade modules needed. The import map
// points the bare specifier `codemirror` at the bundle this emits.
//
// Usage: node build-codemirror-vendor.mjs <install_dir> <out_file> <esbuild_bin>
//   install_dir  dir whose node_modules has @codemirror/* + @lezer/*
//   out_file     where to write the bundle (served as
//                /vendor/codemirror@<v>/codemirror.mjs)
//   esbuild_bin  path to the esbuild binary
import { execFileSync } from 'node:child_process'
import { writeFileSync, mkdirSync, readFileSync } from 'node:fs'
import { join, dirname } from 'node:path'

const [, , installDir, outFile, esbuild] = process.argv
if (!installDir || !outFile || !esbuild) {
  console.error('usage: build-codemirror-vendor.mjs <install_dir> <out_file> <esbuild_bin>')
  process.exit(2)
}
mkdirSync(dirname(outFile), { recursive: true })

// A single entry that re-exports exactly the API the editor uses. esbuild
// bundles the whole @codemirror/* + @lezer/* graph behind it into one module,
// so `@codemirror/state` is included once. Extend this surface when the editor
// needs more - keep it curated so the bundle stays ~React-sized.
const entry = join(installDir, '_cm-entry.js')
writeFileSync(entry, [
  "export {EditorState, EditorSelection, StateField, StateEffect, RangeSet, RangeSetBuilder, Compartment, Prec, Text, Facet, Transaction, Annotation} from '@codemirror/state'",
  "export {EditorView, Decoration, WidgetType, ViewPlugin, keymap, drawSelection, dropCursor, rectangularSelection, crosshairCursor, highlightActiveLine, highlightActiveLineGutter, highlightSpecialChars, placeholder, lineNumbers, gutter} from '@codemirror/view'",
  "export {history, historyKeymap, defaultKeymap, indentWithTab, standardKeymap} from '@codemirror/commands'",
  "export {syntaxTree, HighlightStyle, syntaxHighlighting, defaultHighlightStyle, indentOnInput, bracketMatching, foldGutter, foldKeymap, LanguageSupport, Language} from '@codemirror/language'",
  "export {markdown, markdownLanguage} from '@codemirror/lang-markdown'",
  "export {tags} from '@lezer/highlight'",
].join('\n') + '\n')

// cwd = installDir so esbuild resolves bare `@codemirror/*` from its
// node_modules. No externals -> everything is bundled (singleton state).
execFileSync(esbuild, [
  entry,
  '--bundle',
  '--format=esm',
  '--minify',
  `--outfile=${outFile}`,
], { stdio: 'inherit', cwd: installDir })

// Fail loudly if the public API the import map promises isn't in the bundle -
// a silent miss surfaces as the editor failing to mount in every app. We grep
// the emitted module rather than import() it (some CM view code touches the DOM
// at eval time, which a headless Node import can't provide).
const out = readFileSync(outFile, 'utf8')
const required = [
  'EditorState', 'EditorSelection', 'EditorView', 'Decoration', 'WidgetType',
  'ViewPlugin', 'keymap', 'history', 'defaultKeymap', 'markdown', 'syntaxTree',
  'HighlightStyle', 'syntaxHighlighting', 'tags', 'Transaction', 'Annotation',
]
const missing = required.filter((n) => !out.includes(n))
if (missing.length) {
  console.error('build-codemirror-vendor: bundle missing exports: ' + missing.join(', '))
  process.exit(1)
}
console.log(
  'build-codemirror-vendor: OK (' + required.length + ' core exports present, ' +
  Math.round(out.length / 1024) + ' KB)',
)
