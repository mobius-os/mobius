// Build self-hosted CodeMirror 6 ESM modules for the mini-app import map.
//
// Why this exists: the Notes / LaTeX / Editor / Web Studio mini-apps import
// @codemirror/{state,view,commands,language,lang-markdown}, @lezer/highlight,
// and the `codemirror` meta-package via app-frame.html's (and standalone.py's)
// import map. Serving those from esm.sh meant an OFFLINE-capable app's static
// top-level `import {EditorState} from "@codemirror/state"` had to fetch from a
// third-party CDN before any app code ran — if esm.sh was slow or unreachable
// (offline / flaky network) the dynamic import() of the whole app rejected and
// it rendered nothing (this is the "LaTeX PDF won't load / struggling" bug:
// CodeMirror's failed CDN fetch took the PDF viewer down with it). We self-host
// under /vendor exactly as React / three.js / pdf.js are.
//
// Why a shared core + facades (not per-package bundles with externals):
// CodeMirror REQUIRES a single instance of @codemirror/state and @lezer/common
// (EditorState/NodeType identity is compared across packages). Bundling each
// entry separately would duplicate those shared cores and break editing in
// subtle ways. Bundling ALL import-map specifiers into ONE core.mjs (no
// externals) lets esbuild dedupe every shared dep to exactly one copy; tiny
// facade modules then re-export each namespace, so every import-map specifier
// resolves to that single instance. (CodeMirror is ESM, so unlike React there
// is no __require shim problem — but the single-instance requirement is the
// same, so the same shape applies.)
//
// Usage: node build-codemirror-vendor.mjs <install_dir> <out_dir> <esbuild_bin>
import { execFileSync } from 'node:child_process'
import { writeFileSync, mkdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'

const [, , installDir, outDir, esbuild] = process.argv
if (!installDir || !outDir || !esbuild) {
  console.error('usage: build-codemirror-vendor.mjs <install_dir> <out_dir> <esbuild_bin>')
  process.exit(2)
}
mkdirSync(outDir, { recursive: true })

// Each import-map specifier -> (namespace name in the core, facade filename).
// Keep in sync with the import map in frontend/public/app-frame.html and the
// importmap_block in backend/app/runtime_libs.py / standalone.py.
const SPECS = [
  { spec: 'codemirror', ns: 'cm', file: 'codemirror.mjs' },
  { spec: '@codemirror/state', ns: 'cmState', file: 'state.mjs' },
  { spec: '@codemirror/view', ns: 'cmView', file: 'view.mjs' },
  { spec: '@codemirror/commands', ns: 'cmCommands', file: 'commands.mjs' },
  { spec: '@codemirror/language', ns: 'cmLanguage', file: 'language.mjs' },
  { spec: '@codemirror/lang-markdown', ns: 'cmLangMarkdown', file: 'lang-markdown.mjs' },
  { spec: '@lezer/highlight', ns: 'lezerHighlight', file: 'lezer-highlight.mjs' },
]

// One entry that pulls every specifier into a single graph (deduped once).
const entry = join(installDir, '_cm-core-entry.js')
writeFileSync(entry, SPECS.map(s => `export * as ${s.ns} from "${s.spec}";`).join('\n') + '\n')

// cwd = installDir so esbuild resolves the bare specifiers from its node_modules.
execFileSync(esbuild, [
  entry,
  '--bundle',
  '--format=esm',
  '--define:process.env.NODE_ENV="production"',
  `--outfile=${join(outDir, 'core.mjs')}`,
], { stdio: 'inherit', cwd: installDir })

rmSync(entry, { force: true })

// Mirror exactly what this version provides — no hand-maintained export list.
const core = await import('file://' + join(outDir, 'core.mjs'))

function facade(nsName, ns) {
  const keys = Object.keys(ns)
  const lines = [`import { ${nsName} } from "./core.mjs";`]
  for (const k of keys) {
    if (k === 'default') lines.push(`export default ${nsName}.default;`)
    else lines.push(`export const ${k} = ${nsName}.${k};`)
  }
  if (!keys.includes('default')) lines.push(`export default ${nsName};`)
  return lines.join('\n') + '\n'
}

for (const s of SPECS) {
  writeFileSync(join(outDir, s.file), facade(s.ns, core[s.ns]))
}

// Fail loudly if the public API the import map promises isn't present.
const required = {
  'state.mjs': ['EditorState', 'Compartment', 'StateField'],
  'view.mjs': ['EditorView', 'keymap'],
  'commands.mjs': ['defaultKeymap', 'history'],
  'language.mjs': ['syntaxHighlighting', 'HighlightStyle'],
  'lang-markdown.mjs': ['markdown'],
  'lezer-highlight.mjs': ['tags'],
}
for (const [file, names] of Object.entries(required)) {
  const mod = await import('file://' + join(outDir, file))
  for (const n of names) {
    if (mod[n] === undefined) {
      console.error(`build-codemirror-vendor: ${file} missing export "${n}"`)
      process.exit(1)
    }
  }
}
console.log('build-codemirror-vendor: OK')
