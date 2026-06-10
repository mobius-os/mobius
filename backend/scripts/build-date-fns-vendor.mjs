// Build self-hosted date-fns ESM module for the mini-app import map.
//
// Why this exists: mini-apps import date-fns via app-frame.html's (and
// standalone.py's) import map. Serving it from esm.sh meant an offline-capable
// app that calls `format()` / `parseISO()` etc. depended on a third-party CDN
// fetch before any date formatting could run. We self-host under /vendor to
// make date-fns offline-deterministic, the same rationale as React/CodeMirror.
//
// date-fns is pure JS with no peer deps, so a simple `--bundle` with format=esm
// is sufficient. We export the whole package (no export filter) since
// date-fns tree-shakes fine at module level and the bundle size is reasonable
// (~30-40 KB minified) for what it offers.
//
// Usage: node build-date-fns-vendor.mjs <install_dir> <out_dir> <esbuild_bin>
//   install_dir  dir containing node_modules with date-fns
//   out_dir      where to write date-fns.mjs (served as /vendor/date-fns@<v>/)
//   esbuild_bin  path to the esbuild binary
import { execFileSync } from 'node:child_process'
import { writeFileSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'

const [, , installDir, outDir, esbuild] = process.argv
if (!installDir || !outDir || !esbuild) {
  console.error('usage: build-date-fns-vendor.mjs <install_dir> <out_dir> <esbuild_bin>')
  process.exit(2)
}
mkdirSync(outDir, { recursive: true })

// Re-export everything from the date-fns top-level entry.
const entryPath = join(installDir, '_date-fns-entry.js')
writeFileSync(entryPath, 'export * from "date-fns";\n')

execFileSync(esbuild, [
  entryPath,
  '--bundle',
  '--format=esm',
  '--define:process.env.NODE_ENV="production"',
  `--outfile=${join(outDir, 'date-fns.mjs')}`,
], { stdio: 'inherit', cwd: installDir })

// Verify the core functions apps actually use are exported.
const REQUIRED = ['format', 'parseISO', 'addDays', 'differenceInDays', 'subDays', 'startOfWeek']
const mod = await import('file://' + join(outDir, 'date-fns.mjs'))
const missing = REQUIRED.filter(n => mod[n] === undefined)
if (missing.length) {
  console.error('build-date-fns-vendor: missing exports:', missing)
  process.exit(1)
}
console.log(`build-date-fns-vendor: OK (date-fns — ${Object.keys(mod).length} exports)`)
