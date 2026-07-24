import { readFileSync } from 'node:fs'
import test from 'node:test'
import assert from 'node:assert/strict'

const shellSource = readFileSync(new URL('../Shell.jsx', import.meta.url), 'utf8')

for (const eventType of ['app_build_failed', 'shell_rebuild_failed']) {
  test(`${eventType} does not interrupt the owner UI`, () => {
    const failureBranch = shellSource.match(
      new RegExp(
        String.raw`else if \(ev\.type === '${eventType}'\) \{([\s\S]*?)\n    \}`,
      ),
    )

    assert.ok(failureBranch, 'Shell should explicitly document the silent failure policy')
    assert.doesNotMatch(failureBranch[1], /showToast|setToast|alert\s*\(/,
      'a safe watcher failure must not cover or block the composer')
  })
}
