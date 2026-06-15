// Build self-hosted d3-geo ESM module for the mini-app import map.
//
// Why this exists: the Atlas app imports d3-geo to draw its orthographic
// globe. Served from esm.sh, an offline-capable globe app had to fetch from a
// third-party CDN before the projection could run — a single uncached hop took
// the whole globe down, so Atlas could never be truly offline. We self-host
// under /vendor exactly as React/recharts/date-fns are, removing the last CDN
// dependency on Atlas's render path.
//
// d3-geo is pure JS with no peer deps, so a simple `--bundle` with format=esm
// is sufficient (same shape as date-fns). We export the whole package — Atlas
// imports the whole module namespace (`d3Ref.current = mod`) and reads
// geoOrthographic / geoPath / geoGraticule / geoContains etc. off it.
//
// Usage: node build-d3-geo-vendor.mjs <install_dir> <out_dir> <esbuild_bin>
//   install_dir  dir containing node_modules with d3-geo
//   out_dir      where to write d3-geo.mjs (served as /vendor/d3-geo@<v>/)
//   esbuild_bin  path to the esbuild binary
import { execFileSync } from 'node:child_process'
import { writeFileSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'

const [, , installDir, outDir, esbuild] = process.argv
if (!installDir || !outDir || !esbuild) {
  console.error('usage: build-d3-geo-vendor.mjs <install_dir> <out_dir> <esbuild_bin>')
  process.exit(2)
}
mkdirSync(outDir, { recursive: true })

// Re-export everything from the d3-geo top-level entry.
const entryPath = join(installDir, '_d3-geo-entry.js')
writeFileSync(entryPath, 'export * from "d3-geo";\n')

execFileSync(esbuild, [
  entryPath,
  '--bundle',
  '--format=esm',
  '--define:process.env.NODE_ENV="production"',
  `--outfile=${join(outDir, 'd3-geo.mjs')}`,
], { stdio: 'inherit', cwd: installDir })

// Verify the projection/path functions Atlas actually uses are exported.
const REQUIRED = ['geoOrthographic', 'geoPath', 'geoGraticule', 'geoContains', 'geoDistance']
const mod = await import('file://' + join(outDir, 'd3-geo.mjs'))
const missing = REQUIRED.filter(n => mod[n] === undefined)
if (missing.length) {
  console.error('build-d3-geo-vendor: missing exports:', missing)
  process.exit(1)
}
console.log(`build-d3-geo-vendor: OK (d3-geo — ${Object.keys(mod).length} exports)`)
