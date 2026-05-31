// Build self-hosted React ESM modules for the mini-app import map.
//
// Why this exists: mini-apps import react / react-dom / react-dom/client /
// react/jsx-runtime via app-frame.html's (and standalone.py's) import map.
// Serving those from esm.sh meant offline-capable apps depended on a
// third-party CDN whose React entry is a multi-hop re-export chain, cached
// only opportunistically by the service worker — so an offline app could go
// blank on its top-level `import 'react-dom/client'`. We self-host React
// under /vendor, exactly as three.js is, to make offline deterministic.
//
// Why a shared core + facades (not four independent bundles): react and
// react-dom ship as CommonJS. Bundling each entry separately with
// `--external:react` makes esbuild emit a throwing `__require("react")`
// shim ("Dynamic require of \"react\" is not supported") in the browser —
// the external never becomes a real import-map import, so `createRoot` &
// friends are missing at runtime. Instead we bundle ALL four entries into
// ONE `core.mjs` (no externals) so React is included exactly once, then
// generate tiny facade modules that re-export the core's namespaces. Every
// import-map specifier resolves to that single React instance, so hooks
// work across the react / react-dom boundary.
//
// Usage: node build-react-vendor.mjs <install_dir> <out_dir> <esbuild_bin>
//   install_dir  dir containing node_modules with react + react-dom
//   out_dir      where to write core.mjs + the four facades (served as
//                /vendor/react@<v>/*.mjs)
//   esbuild_bin  path to the esbuild binary
import { execFileSync } from 'node:child_process'
import { writeFileSync, mkdirSync, rmSync } from 'node:fs'
import { join } from 'node:path'

const [, , installDir, outDir, esbuild] = process.argv
if (!installDir || !outDir || !esbuild) {
  console.error('usage: build-react-vendor.mjs <install_dir> <out_dir> <esbuild_bin>')
  process.exit(2)
}
mkdirSync(outDir, { recursive: true })

// The core entry pulls every specifier the import map exposes into one
// graph. Kept in installDir (a throwaway tmp dir) so it never lands in the
// served /vendor output.
const entry = join(installDir, '_react-core-entry.js')
writeFileSync(entry, [
  'export * as react from "react";',
  'export * as reactDom from "react-dom";',
  'export * as reactDomClient from "react-dom/client";',
  'export * as jsxRuntime from "react/jsx-runtime";',
].join('\n') + '\n')

// cwd = installDir so esbuild resolves bare `react` from its node_modules.
execFileSync(esbuild, [
  entry,
  '--bundle',
  '--format=esm',
  '--define:process.env.NODE_ENV="production"',
  `--outfile=${join(outDir, 'core.mjs')}`,
], { stdio: 'inherit', cwd: installDir })

rmSync(entry, { force: true })

// Read the real exports off the built core so each facade mirrors exactly
// what this React version provides — no hand-maintained export list to
// drift on a version bump.
const core = await import('file://' + join(outDir, 'core.mjs'))

function facade(nsName, ns) {
  const keys = Object.keys(ns)
  const lines = [`import { ${nsName} } from "./core.mjs";`]
  for (const k of keys) {
    if (k === 'default') lines.push(`export default ${nsName}.default;`)
    else lines.push(`export const ${k} = ${nsName}.${k};`)
  }
  // react-dom/client has no default export of its own; give the facade one
  // anyway (the namespace) so `import X from 'react-dom/client'` never fails.
  if (!keys.includes('default')) lines.push(`export default ${nsName};`)
  return lines.join('\n') + '\n'
}

writeFileSync(join(outDir, 'react.mjs'), facade('react', core.react))
writeFileSync(join(outDir, 'react-dom.mjs'), facade('reactDom', core.reactDom))
writeFileSync(join(outDir, 'client.mjs'), facade('reactDomClient', core.reactDomClient))
writeFileSync(join(outDir, 'jsx-runtime.mjs'), facade('jsxRuntime', core.jsxRuntime))

// Fail the build loudly if the public API the import map promises isn't
// present — a silent miss here would surface as every mini-app breaking.
const required = {
  'react.mjs': ['useState', 'createElement', 'version'],
  'client.mjs': ['createRoot'],
  'jsx-runtime.mjs': ['jsx', 'jsxs', 'Fragment'],
}
for (const [file, names] of Object.entries(required)) {
  const mod = await import('file://' + join(outDir, file))
  for (const n of names) {
    if (mod[n] === undefined) {
      console.error(`build-react-vendor: ${file} missing export "${n}"`)
      process.exit(1)
    }
  }
}
console.log('build-react-vendor: OK (react ' + core.react.version + ')')
