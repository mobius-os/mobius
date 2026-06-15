// Build self-hosted DOMPurify ESM module for the mini-app import map.
//
// Why this exists: HTML-rendering mini-apps (the Notes preview) must sanitize
// untrusted Markdown-derived HTML with DOMPurify before injecting it. Served
// from esm.sh, an offline-capable app depended on a third-party CDN fetch
// before it could sanitize anything. We self-host under /vendor exactly as
// recharts/date-fns are, so offline is deterministic.
//
// DOMPurify is pure JS with no peer deps. It needs a DOM at RUNTIME (it binds
// to window/document when first called), but bundling is DOM-free. The default
// export is a factory/instance exposing `sanitize`. A simple `--bundle` with
// format=esm is sufficient.
//
// Usage: node build-dompurify-vendor.mjs <install_dir> <out_dir> <esbuild_bin>
//   install_dir  dir containing node_modules with dompurify
//   out_dir      where to write dompurify.mjs (served as /vendor/dompurify@<v>/)
//   esbuild_bin  path to the esbuild binary
import { execFileSync } from 'node:child_process'
import { writeFileSync, mkdirSync, readFileSync } from 'node:fs'
import { join } from 'node:path'

const [, , installDir, outDir, esbuild] = process.argv
if (!installDir || !outDir || !esbuild) {
  console.error('usage: build-dompurify-vendor.mjs <install_dir> <out_dir> <esbuild_bin>')
  process.exit(2)
}
mkdirSync(outDir, { recursive: true })

// Re-export the default DOMPurify export from the top-level entry.
const entryPath = join(installDir, '_dompurify-entry.js')
writeFileSync(entryPath, 'export { default } from "dompurify";\n')

execFileSync(esbuild, [
  entryPath,
  '--bundle',
  '--format=esm',
  '--define:process.env.NODE_ENV="production"',
  `--outfile=${join(outDir, 'dompurify.mjs')}`,
], { stdio: 'inherit', cwd: installDir })

// SHAPE check only: DOMPurify needs a real DOM (window/document) to actually
// sanitize. Outside a browser it returns a factory whose `sanitize` is a
// function until a window is supplied; we therefore assert the default export
// is present and `sanitize` is a function, and do NOT treat a no-op sanitize
// (the documented behavior without a DOM) as a build failure. At image time
// and in the browser it has a DOM and sanitizes for real.
const builtSrc = readFileSync(join(outDir, 'dompurify.mjs'), 'utf8')
if (builtSrc.trim().length === 0) {
  console.error('build-dompurify-vendor: built file is empty')
  process.exit(1)
}
const mod = await import('file://' + join(outDir, 'dompurify.mjs'))
const dp = mod.default
if (dp === undefined) {
  console.error('build-dompurify-vendor: no default export in built file')
  process.exit(1)
}
// Without a DOM, DOMPurify's default export is the factory function (which
// also carries a `sanitize` that is a function). Accept either the bound
// instance (dp.sanitize is a function) or the factory (typeof dp === 'function').
const sanitizeOk =
  typeof dp.sanitize === 'function' || typeof dp === 'function'
if (!sanitizeOk) {
  console.error(
    'build-dompurify-vendor: default export has no `sanitize` function and is not a factory',
  )
  process.exit(1)
}
console.log('build-dompurify-vendor: OK (dompurify — default export with sanitize, shape-checked)')
