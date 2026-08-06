// Build-output gate for the offline feature. Run after `npm run build`
// from the frontend/ directory: `node scripts/check-offline-build.mjs [dir]`.
//
// Asserts the two offline-critical static files made it into the build
// AND into the service worker's precache manifest. If offline.html
// isn't precached, the SW catch handler silently falls back to a
// network error and the browser-chrome leak we're killing comes back;
// if mobius-runtime.js isn't precached, offline-capable apps can't load
// window.mobius offline.
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { DOCUMENT_REVISION_MARK, UNPRECACHED_WORKERS } from './precache-policy.mjs'

const buildDir = path.resolve(process.argv[2] || 'dist')
const sw = readFileSync(path.join(buildDir, 'sw.js'), 'utf8')
const required = ['offline.html', 'mobius-runtime.js']
for (const f of required) {
  if (!existsSync(path.join(buildDir, f))) {
    throw new Error(`missing ${path.join(buildDir, f)}`)
  }
  if (!sw.includes(f)) throw new Error(`${f} not precached in dist/sw.js`)
}

// Push-worker gate. Android hands a web push to the installed shell only when
// the push worker's SCOPE sits inside the PWA manifest's scope, so those two
// values are a cross-file contract with no import to hold them together —
// break it and every notification silently becomes a Chrome notification whose
// tap leaves the app. Invisible on desktop, in headless runs, and in CI, so
// this is the only place it can be caught. Derive the expected scope from the
// real manifest and prove the shipped bundle registers exactly that.
const pushWorker = 'sw-push.js'
// Workers run under the CSP of their own response and a precached response
// freezes its headers, so none of them may be precached. See
// scripts/precache-policy.mjs for the full reasoning.
for (const worker of UNPRECACHED_WORKERS) {
  if (!existsSync(path.join(buildDir, worker))) {
    throw new Error(`missing ${path.join(buildDir, worker)} — that feature is dead`)
  }
  if (sw.includes(worker)) {
    throw new Error(`${worker} must NOT be precached in dist/sw.js`)
  }
}

// Documents carry the CSP that also governs the workers they spawn, so their
// precache revision must change every build or a policy fix can never reach an
// installed PWA.
const unstampedDocument = [...sw.matchAll(/"revision":"([^"]*)","url":"([^"]+\.html)"/g)]
  .find(([, revision]) => !revision.includes(DOCUMENT_REVISION_MARK))
if (unstampedDocument) {
  throw new Error(
    `precached document ${unstampedDocument[2]} has an unstamped revision — `
    + 'its headers would freeze at install time',
  )
}
const manifest = JSON.parse(
  readFileSync(path.join(buildDir, 'manifest.webmanifest'), 'utf8'),
)
const pushScope = `${manifest.scope}push/`
const assets = path.join(buildDir, 'assets')
const registersPushScope = readdirSync(assets)
  .filter(f => f.endsWith('.js'))
  .some(f => readFileSync(path.join(assets, f), 'utf8').includes(pushScope))
if (!registersPushScope) {
  throw new Error(
    `no bundle registers the push worker at ${pushScope} — the manifest scope `
    + `(${manifest.scope}) and PUSH_SW_SCOPE have drifted`,
  )
}

console.log('offline build OK:', required.join(', '), 'present + precached')
console.log(`push worker OK: scoped ${pushScope}`)
console.log(`workers OK: ${UNPRECACHED_WORKERS.join(', ')} shipped, unprecached`)
