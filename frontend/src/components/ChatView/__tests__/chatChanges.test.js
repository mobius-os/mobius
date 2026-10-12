import test from 'node:test'
import assert from 'node:assert/strict'
import {
  chatChanges,
  compactChangesSummary,
  groupChangedFiles,
} from '../chatChanges.js'

function entry(id, ...paths) {
  return {
    id,
    preview: {
      files: paths.map(path => ({ path, hunks: [] })),
      truncated: false,
    },
  }
}

test('every recorded source edit is listed, whatever happened to it since', () => {
  const changes = chatChanges([
    entry('one', '/data/platform/a.js', '/data/platform/b.js'),
    entry('two', '/data/apps/demo/index.jsx'),
  ])

  assert.deepEqual(changes.files.map(file => file.path), [
    '/data/apps/demo/index.jsx', '/data/platform/a.js', '/data/platform/b.js',
  ])
  assert.equal(changes.entries.length, 2)
  assert.equal(compactChangesSummary(changes), '3 files changed')
  assert.equal(compactChangesSummary(chatChanges([])), 'No file changes yet')
  assert.equal(compactChangesSummary(chatChanges([entry('x', '/data/platform/a.js')])), '1 file changed')
})

test('repeated edits become one file row while retaining every diff hunk', () => {
  const first = entry('first', '/data/platform/repeated.js')
  first.preview.files[0] = {
    path: '/data/platform/repeated.js', status: 'A', insertions: 2, deletions: 0,
    hunks: [{ header: 'first' }],
  }
  const second = entry('second', '/data/platform/repeated.js')
  second.preview.files[0] = {
    path: '/data/platform/repeated.js', status: 'M', insertions: 1, deletions: 1,
    hunks: [{ header: 'second' }],
  }
  const changes = chatChanges([first, second])

  assert.equal(changes.entries.length, 2)
  assert.deepEqual(changes.files, [{
    path: '/data/platform/repeated.js',
    status: 'A',
    insertions: 3,
    deletions: 1,
    hunks: [{ header: 'first' }, { header: 'second' }],
  }])
})

test('changed files group by owning project', () => {
  assert.deepEqual(groupChangedFiles([
    { path: '/data/apps/notes/index.jsx' },
    { path: '/data/platform/frontend/a.jsx' },
    { path: '/data/apps/notes/theme.js' },
  ]).map(group => [group.id, group.files.length]), [
    ['/data/platform', 1],
    ['/data/apps/notes', 2],
  ])
})

test('Changes keeps source work while excluding non-source and traversing paths', () => {
  const projectId = '123e4567-e89b-42d3-a456-426614174000'
  const changes = chatChanges([
    entry('source', '/data/platform/frontend/a.jsx'),
    entry('app-source', '/data/apps/notes/index.jsx'),
    entry('project-source', `/data/projects/${projectId}/src/main.js`),
    entry('tmp', '/tmp/contrib-review/app/a.jsx'),
    entry('review', '/data/contrib/private-review/worktree/a.jsx'),
    entry('app-data', '/data/apps/80/settings.json'),
    entry('credential', '/data/.secret-key'),
    entry('outside', '/etc/passwd'),
    entry('traversal', '/data/apps/notes/../80/settings.json'),
    entry('platform-traversal', '/data/platform/../.secret-key'),
    { ...entry('rename-from-private', '/data/platform/copied.txt'), preview: {
      files: [{ path: '/data/platform/copied.txt', oldPath: '/data/.secret-key',
        newPath: '/data/platform/copied.txt', hunks: [] }],
    } },
  ])

  assert.deepEqual(changes.files.map(file => file.path), [
    '/data/apps/notes/index.jsx', '/data/platform/frontend/a.jsx',
    `/data/projects/${projectId}/src/main.js`,
  ])
  assert.equal(changes.entries.length, 3)
  assert.deepEqual(changes.groups.map(group => [group.id, group.files.length]), [
    ['/data/platform', 1], ['/data/apps/notes', 1],
    [`/data/projects/${projectId}`, 1],
  ])
})

test('excerpt-only edits and the latest edit time are reported', () => {
  const older = { ...entry('older', '/data/platform/a.js'), ts: 10 }
  older.preview.truncated = true
  const newer = { ...entry('newer', '/data/platform/b.js'), ts: 20 }
  const changes = chatChanges([older, newer])
  assert.equal(changes.excerptCount, 1)
  assert.equal(changes.latestTs, 20)
})
