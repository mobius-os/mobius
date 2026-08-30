import { test } from 'node:test'
import assert from 'node:assert/strict'
import {
  canInitializeProjectGit,
  gitAnnotationForEntry,
  gitChangeCount,
  gitIdentityLabel,
  gitStatusPresentation,
  remoteSyncActions,
  remoteSyncPresentation,
} from '../projectGit.js'

const changes = [
  { path: 'README.md', status: 'modified', staged: false },
  { path: 'src/app.js', status: 'added', staged: true },
  { path: 'src/lib/new.js', status: 'untracked', staged: false },
]

test('file annotations preserve concise Git status and readable meaning', () => {
  assert.deepEqual(
    gitAnnotationForEntry(changes, { path: 'README.md', type: 'file' }),
    { kind: 'file', count: 1, status: 'modified', staged: false, code: 'M', label: 'Modified' },
  )
  assert.equal(gitStatusPresentation('conflict').code, '!')
  assert.equal(gitAnnotationForEntry(changes, { path: 'clean.md', type: 'file' }), null)
})

test('folders aggregate all changed descendants without inventing a status', () => {
  assert.deepEqual(
    gitAnnotationForEntry(changes, { path: 'src', type: 'directory' }),
    { kind: 'directory', count: 2, code: '2', label: '2 changed' },
  )
  assert.equal(gitAnnotationForEntry(changes, { path: 'docs', type: 'directory' }), null)
})

test('repository identity and totals are robust to clean and detached states', () => {
  assert.equal(gitChangeCount({ available: true, counts: { modified: 2, untracked: 1 } }), 3)
  assert.equal(gitChangeCount({ available: false, counts: { modified: 2 } }), 0)
  assert.equal(gitIdentityLabel({ available: true, branch: 'main', head: '12345678' }), 'main')
  assert.equal(gitIdentityLabel({ available: true, branch: null, head: '12345678' }), '12345678')
})

test('project versioning remains reachable inside the shared data repository', () => {
  assert.equal(canInitializeProjectGit(undefined), true)
  assert.equal(canInitializeProjectGit({ available: false, repository_scope: null }), true)
  assert.equal(canInitializeProjectGit({ available: true, repository_scope: 'shared' }), true)
  assert.equal(canInitializeProjectGit({ available: true, repository_scope: 'project' }), false)
})

test('remote synchronization exposes only safe fast-forward actions', () => {
  const ready = {
    available: true, connected: true, github_connected: true,
    ahead: 2, behind: 0, dirty: false, diverged: false,
  }
  assert.equal(remoteSyncPresentation(ready).title, '2 commits ready to push')
  assert.deepEqual(remoteSyncActions(ready), { fetch: true, pull: false, push: true })
  assert.deepEqual(
    remoteSyncActions({ ...ready, behind: 1, diverged: true }),
    { fetch: true, pull: false, push: false },
  )
  assert.deepEqual(
    remoteSyncActions({ ...ready, ahead: 0, behind: 1 }),
    { fetch: true, pull: true, push: false },
  )
  assert.equal(remoteSyncActions({ ...ready, dirty: true }).push, false)
  assert.equal(remoteSyncActions({ ...ready, github_connected: false }).fetch, false)
})
