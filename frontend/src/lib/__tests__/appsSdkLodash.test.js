// Defense-in-depth for the lodash supply-chain risk. @openai/apps-sdk-ui pulls
// lodash transitively, only through its `Slider` component — which the shell
// does not import, so lodash is tree-shaken out of the shipped bundle entirely
// (verified: an esbuild bundle of the apps-sdk-ui components we DO import
// contains zero lodash). lodash is ALSO pinned to a patched 4.18.1 via
// `overrides` in package.json. This test is the second layer: it fails the
// moment the shell imports `Slider`, which would start shipping lodash and make
// the bundle depend on the override alone. See ARCHITECTURE.md "Security updates".
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
    'Importing apps-sdk-ui Slider ships lodash in the bundle (it is otherwise ' +
      'tree-shaken out). lodash is pinned to a patched version via overrides, ' +
      'but keep it out of the bundle anyway. See ARCHITECTURE.md "Security updates".',
  )
})
