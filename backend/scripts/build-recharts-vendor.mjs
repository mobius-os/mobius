// Build self-hosted recharts ESM module for the mini-app import map.
//
// Why this exists: mini-apps that use recharts import it via app-frame.html's
// (and standalone.py's) import map. Serving it from esm.sh meant an offline-
// capable app that renders a chart had to fetch from a third-party CDN before
// any chart component rendered — a single uncached hop took the whole chart
// subtree down. We self-host under /vendor exactly as React/CodeMirror are.
//
// recharts depends on react + react-dom (peer deps). We mark them external
// so esbuild generates `import ... from "react"` / `import ... from
// "react-dom"` — these are already in the importmap pointing at /vendor/react,
// so the single React instance is shared across the shell and all charts.
// Unlike the React/CodeMirror case (where CommonJS + single-instance
// requirements forced a shared-core+facade shape), recharts is ESM-friendly
// and tree-shakeable; a single flat bundle with externals is correct here.
//
// Usage: node build-recharts-vendor.mjs <install_dir> <out_dir> <esbuild_bin>
//   install_dir  dir containing node_modules with recharts + date-fns
//   out_dir      where to write recharts.mjs (served as /vendor/recharts@<v>/)
//   esbuild_bin  path to the esbuild binary
import { execFileSync } from 'node:child_process'
import { writeFileSync, mkdirSync } from 'node:fs'
import { join } from 'node:path'

const [, , installDir, outDir, esbuild] = process.argv
if (!installDir || !outDir || !esbuild) {
  console.error('usage: build-recharts-vendor.mjs <install_dir> <out_dir> <esbuild_bin>')
  process.exit(2)
}
mkdirSync(outDir, { recursive: true })

// The full named export list from the current importmap URL. This mirrors
// the `?exports=` filter on the old esm.sh URL so the bundle only includes
// what is actually advertised; apps importing anything else get a clear
// "not exported" error rather than a silently-missing component.
const EXPORTS = [
  'LineChart', 'BarChart', 'PieChart', 'AreaChart',
  'Line', 'Bar', 'Pie', 'Area',
  'XAxis', 'YAxis', 'ZAxis', 'Tooltip', 'CartesianGrid', 'Legend',
  'ResponsiveContainer', 'Cell', 'LabelList', 'Brush',
  'ComposedChart', 'ScatterChart', 'Scatter',
  'RadarChart', 'Radar', 'PolarGrid', 'PolarAngleAxis', 'PolarRadiusAxis',
  'RadialBarChart', 'RadialBar',
]

const entryPath = join(installDir, '_recharts-entry.js')
writeFileSync(
  entryPath,
  EXPORTS.map(n => `export { ${n} } from "recharts";`).join('\n') + '\n',
)

execFileSync(esbuild, [
  entryPath,
  '--bundle',
  '--format=esm',
  '--define:process.env.NODE_ENV="production"',
  '--external:react',
  '--external:react-dom',
  `--outfile=${join(outDir, 'recharts.mjs')}`,
], { stdio: 'inherit', cwd: installDir })

// Verify required named exports appear in the bundle source (the built file
// externals 'react' so a live dynamic import() fails in node; check the
// source text instead).
import { readFileSync } from 'node:fs'
const builtSrc = readFileSync(join(outDir, 'recharts.mjs'), 'utf8')
const missing = EXPORTS.filter(n => {
  // Each exported name appears as `export { ... <n> ... }` or
  // `export const <n> =` in the esbuild output.
  return !builtSrc.includes(n)
})
if (missing.length) {
  console.error('build-recharts-vendor: missing exports in built file:', missing)
  process.exit(1)
}
console.log(`build-recharts-vendor: OK (recharts — ${EXPORTS.length} components)`)
