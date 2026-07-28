// Build-output gate for the offline feature. Run after `npm run build`
// from the frontend/ directory: `node scripts/check-offline-build.mjs [dir]`.
//
// Asserts the two offline-critical static files made it into the build
// AND into the service worker's precache manifest. If offline.html
// isn't precached, the SW catch handler silently falls back to a
// network error and the browser-chrome leak we're killing comes back;
// if mobius-runtime.js isn't precached, offline-capable apps can't load
// window.mobius offline.
import { existsSync, readFileSync } from 'node:fs'
import path from 'node:path'

const buildDir = path.resolve(process.argv[2] || 'dist')
const sw = readFileSync(path.join(buildDir, 'sw.js'), 'utf8')
const required = ['offline.html', 'mobius-runtime.js']
for (const f of required) {
  if (!existsSync(path.join(buildDir, f))) {
    throw new Error(`missing ${path.join(buildDir, f)}`)
  }
  if (!sw.includes(f)) throw new Error(`${f} not precached in dist/sw.js`)
}
console.log('offline build OK:', required.join(', '), 'present + precached')
