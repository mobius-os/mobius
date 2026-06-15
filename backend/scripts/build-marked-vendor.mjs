// Build self-hosted marked ESM module for the mini-app import map.
//
// Why this exists: the Notes app imports marked to render markdown note-card
// previews. Served from esm.sh, an offline-capable Notes app had to fetch from
// a third-party CDN before any preview could render — the same offline
// liability we removed for React/CodeMirror/recharts. We self-host under
// /vendor so markdown rendering is offline-deterministic.
//
// marked is pure JS with no peer deps, so a simple `--bundle` with format=esm
// is sufficient (same shape as date-fns / d3-geo). We export the whole package;
// Notes imports the named `marked` function (`m.marked`), and `parse` is the
// other common entry point apps reach for.
//
// Usage: node build-marked-vendor.mjs <install_dir> <out_dir> <esbuild_bin>
//   install_dir  dir containing node_modules with marked
//   out_dir      where to write marked.mjs (served as /vendor/marked@<v>/)
//   esbuild_bin  path to the esbuild binary
import { execFileSync } from 'node:child_process'
import { writeFileSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'

const [, , installDir, outDir, esbuild] = process.argv
if (!installDir || !outDir || !esbuild) {
  console.error('usage: build-marked-vendor.mjs <install_dir> <out_dir> <esbuild_bin>')
  process.exit(2)
}
mkdirSync(outDir, { recursive: true })

// Re-export everything from the marked top-level entry.
const entryPath = join(installDir, '_marked-entry.js')
writeFileSync(entryPath, 'export * from "marked";\n')

execFileSync(esbuild, [
  entryPath,
  '--bundle',
  '--format=esm',
  '--define:process.env.NODE_ENV="production"',
  `--outfile=${join(outDir, 'marked.mjs')}`,
], { stdio: 'inherit', cwd: installDir })

// Verify the entry points apps actually use are exported, and that `marked`
// is callable as a function (Notes does `marked(text, opts)`).
const REQUIRED = ['marked', 'parse', 'lexer']
const mod = await import('file://' + join(outDir, 'marked.mjs'))
const missing = REQUIRED.filter(n => mod[n] === undefined)
if (missing.length) {
  console.error('build-marked-vendor: missing exports:', missing)
  process.exit(1)
}
if (typeof mod.marked !== 'function') {
  console.error('build-marked-vendor: `marked` export is not a function')
  process.exit(1)
}
console.log(`build-marked-vendor: OK (marked — ${Object.keys(mod).length} exports)`)
