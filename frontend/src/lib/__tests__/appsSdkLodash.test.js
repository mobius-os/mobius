// Guards the one npm-audit HIGH we knowingly accept: lodash (4.17.21 — the
// latest published version, and still vulnerable; there is no upstream fix).
// lodash enters the dependency tree ONLY through @openai/apps-sdk-ui's `Slider`
// component, which the shell does not import — so Vite/rollup tree-shakes lodash
// out of the shipped bundle entirely (verified: an esbuild bundle of the
// apps-sdk-ui components we DO import contains zero lodash). The vulnerable
// _.template / _.unset / _.omit therefore never reach the browser.
//
// If the shell ever imports `Slider`, lodash would start shipping and the
// reachability rationale in ARCHITECTURE.md ("Security updates") would silently
// rot. This test fails the moment that happens.
import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync, readdirSync } from 'node:fs'
import { join, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const SRC = join(dirname(fileURLToPath(import.meta.url)), '..', '..')

function* walkSource(dir) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    if (entry.name === 'node_modules' || entry.name === '__tests__') continue
    const full = join(dir, entry.name)
    if (entry.isDirectory()) yield* walkSource(full)
    else if (/\.(jsx?|tsx?)$/.test(entry.name)) yield full
  }
}

test('shell does not import the apps-sdk-ui Slider (its only lodash carrier)', () => {
  const offenders = []
  for (const file of walkSource(SRC)) {
    if (/@openai\/apps-sdk-ui\/components\/Slider/.test(readFileSync(file, 'utf8'))) {
      offenders.push(file)
    }
  }
  assert.deepEqual(
    offenders,
    [],
    'Importing apps-sdk-ui Slider pulls lodash (HIGH advisory, no upstream fix) ' +
      'into the shipped bundle. See ARCHITECTURE.md "Security updates". Use a ' +
      'different control or vendor a lodash-free slider instead.',
  )
})
