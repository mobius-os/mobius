import test from 'node:test'
import assert from 'node:assert/strict'

import {
  fileStatusLabel,
  shortSha,
  summarizePreview,
  isTrivialUpdate,
  hasReviewableChanges,
} from '../platformUpdatePreview.js'

test('fileStatusLabel maps git status letters, falls back gracefully', () => {
  assert.equal(fileStatusLabel('A'), 'Added')
  assert.equal(fileStatusLabel('M'), 'Modified')
  assert.equal(fileStatusLabel('D'), 'Removed')
  assert.equal(fileStatusLabel('R100'), 'Renamed')
  assert.equal(fileStatusLabel('x'), 'Changed')
  assert.equal(fileStatusLabel(''), 'Changed')
  assert.equal(fileStatusLabel(undefined), 'Changed')
})

test('shortSha truncates and tolerates junk', () => {
  assert.equal(shortSha('0123456789abcdef'), '01234567')
  assert.equal(shortSha('abc'), 'abc')
  assert.equal(shortSha(''), '')
  assert.equal(shortSha(null), '')
})

test('summarizePreview totals files/commits and skips binary counts', () => {
  const preview = {
    commits: [{ sha: 'a', subject: 'one' }, { sha: 'b', subject: 'two' }],
    files: [
      { path: 'a.py', status: 'M', insertions: 3, deletions: 1 },
      { path: 'img.png', status: 'A', insertions: null, deletions: null },
      { path: 'b.py', status: 'D', insertions: 0, deletions: 12 },
    ],
    diff: 'diff --git ...',
    diff_truncated: true,
  }
  const s = summarizePreview(preview)
  assert.equal(s.commitCount, 2)
  assert.equal(s.fileCount, 3)
  assert.equal(s.insertions, 3)
  assert.equal(s.deletions, 13)
  assert.equal(s.hasDiff, true)
  assert.equal(s.diffTruncated, true)
})

test('summarizePreview is safe on empty/absent fields', () => {
  const s = summarizePreview({})
  assert.deepEqual(s, {
    commitCount: 0,
    fileCount: 0,
    insertions: 0,
    deletions: 0,
    hasDiff: false,
    diffTruncated: false,
  })
  // Fully undefined input must not throw.
  assert.equal(summarizePreview(undefined).fileCount, 0)
})

test('isTrivialUpdate is true only when no files changed', () => {
  assert.equal(isTrivialUpdate({ files: [] }), true)
  assert.equal(isTrivialUpdate({}), true)
  assert.equal(isTrivialUpdate({ files: [{ path: 'a', status: 'M' }] }), false)
})

test('hasReviewableChanges: files OR commits make it reviewable', () => {
  assert.equal(hasReviewableChanges({ files: [], commits: [] }), false)
  assert.equal(hasReviewableChanges({ files: [{ path: 'a' }], commits: [] }), true)
  // A version bump with commits but no tree delta is still worth listing.
  assert.equal(hasReviewableChanges({ files: [], commits: [{ sha: 'a' }] }), true)
})
