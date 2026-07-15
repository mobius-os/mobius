import { readFileSync } from 'node:fs'
import test from 'node:test'
import assert from 'node:assert/strict'

const shellSource = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')

test('automatic shell rebuild failures do not interrupt the owner UI', () => {
  const failureBranch = shellSource.match(
    /else if \(ev\.type === 'shell_rebuild_failed'\) \{([\s\S]*?)\n    \}/,
  )

  assert.ok(failureBranch, 'Shell should explicitly document the silent failure policy')
  assert.doesNotMatch(failureBranch[1], /showToast|setToast|alert\s*\(/,
    'a safe watcher failure must not cover or block the composer')
})
