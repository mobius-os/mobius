import test from 'node:test'
import assert from 'node:assert/strict'
import {
  chatChangesOverview,
  compactChangesSummary,
  contributionNeedsAttention,
  contributionSourceFile,
  contributionStage,
  initialChangesStage,
  isUnsortedDismissed,
  rememberUnsortedDismissed,
  unsortedDismissKey,
} from '../chatChangesLifecycle.js'

function entry(id, ...paths) {
  return {
    id,
    preview: {
      files: paths.map(path => ({ path, hunks: [] })),
      truncated: false,
    },
  }
}

function fakeStorage() {
  const values = new Map()
  return {
    getItem: key => values.get(key) || null,
    setItem: (key, value) => values.set(key, String(value)),
  }
}

test('one lifecycle separates recorded edits from prepared, open, and landed work', () => {
  const overview = chatChangesOverview([
    entry('one', '/data/platform/a.js', '/data/platform/b.js'),
    entry('two', '/data/apps/demo/index.jsx'),
  ], { records: [
    {
      id: 'prepared', status: 'prepared', source_root: '/data/platform',
      files: ['a.js'], updated_at: '2026-08-27T10:00:00Z',
    },
    {
      id: 'open', status: 'open', source_root: '/data/apps/demo',
      files: ['index.jsx'], updated_at: '2026-08-27T11:00:00Z',
    },
    {
      id: 'landed', status: 'merged', source_root: '/data/platform',
      files: ['old.js'], updated_at: '2026-08-27T09:00:00Z',
    },
  ] })

  assert.deepEqual(overview.unsortedPaths, ['/data/platform/b.js'])
  assert.deepEqual(overview.unsortedFiles.map(file => file.path), ['/data/platform/b.js'])
  assert.deepEqual(
    overview.unsortedEntries.map(item => item.preview.files.map(file => file.path)),
    [['/data/platform/b.js']],
  )
  assert.deepEqual(overview.counts, {
    unsorted: 1, prepared: 1, open: 1, landed: 1,
    attention: 0, files: 3, updates: 2,
  })
  assert.equal(compactChangesSummary(overview), '1 unsorted · 1 prepared · 1 open')
  assert.equal(initialChangesStage(overview), 'unsorted')
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

  const overview = chatChangesOverview([first, second], { records: [] })

  assert.equal(overview.counts.unsorted, 1)
  assert.equal(overview.unsortedEntries.length, 2)
  assert.deepEqual(overview.unsortedFiles, [{
    path: '/data/platform/repeated.js',
    status: 'A',
    insertions: 3,
    deletions: 1,
    hunks: [{ header: 'first' }, { header: 'second' }],
  }])
})

test('coverage uses the exact source root and retains a Möbius fallback for older records', () => {
  assert.equal(
    contributionSourceFile({ source_root: '/workspace/project' }, 'src/a.js'),
    '/workspace/project/src/a.js',
  )
  assert.equal(
    contributionSourceFile({ repo: 'mobius-os/mobius' }, 'frontend/a.jsx'),
    '/data/platform/frontend/a.jsx',
  )
  assert.equal(
    contributionSourceFile({ repo: 'mobius-os/app-habits' }, 'index.jsx'),
    '/data/apps/habits/index.jsx',
  )
  assert.equal(contributionSourceFile({ repo: 'someone/project' }, 'a.js'), '')
})

test('status and attention semantics stay independent', () => {
  assert.equal(contributionStage({ status: 'submitting' }), 'prepared')
  assert.equal(contributionStage({ status: 'landing' }), 'open')
  assert.equal(contributionStage({ status: 'closed' }), 'landed')
  assert.equal(contributionStage({ status: 'abandoned' }), null)
  assert.equal(contributionNeedsAttention({ status: 'open' }), false)
  assert.equal(contributionNeedsAttention({ needs_attention: true }), true)
  assert.equal(contributionNeedsAttention({ review: { state: 'needs_refresh' } }), true)
  assert.equal(contributionNeedsAttention({ last_submit_error: 'Moved' }), true)
})

test('Brain copy stays quiet when settled and names only useful outstanding work', () => {
  assert.equal(compactChangesSummary({ counts: { landed: 3 } }), '3 landed · everything settled')
  assert.equal(compactChangesSummary({ counts: {} }), 'No changes from this chat yet')
  assert.equal(
    compactChangesSummary({ counts: { unsorted: 2, prepared: 1, open: 4, attention: 1 } }),
    '2 unsorted · 1 prepared · 4 open · 1 need attention',
  )
})

test('dismissing the preparation suggestion hides one revision, not future edits', () => {
  const storage = fakeStorage()
  const revision = 'edit-1:/data/platform/a.js'
  assert.match(unsortedDismissKey('chat-a', revision), /^mobius:changes-dismissed:chat-a:/)
  assert.equal(isUnsortedDismissed('chat-a', revision, storage), false)
  assert.equal(rememberUnsortedDismissed('chat-a', revision, storage), true)
  assert.equal(isUnsortedDismissed('chat-a', revision, storage), true)
  assert.equal(isUnsortedDismissed('chat-a', `${revision}|edit-2`, storage), false)
})
