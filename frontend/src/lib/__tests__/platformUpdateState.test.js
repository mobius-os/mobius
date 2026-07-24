import assert from 'node:assert/strict'
import test from 'node:test'
import {
  platformStatusFromApply,
  platformUpdateStatusLabel,
} from '../platformUpdateState.js'

test('a clean apply consumes the reviewed target but preserves restart readiness', () => {
  const projected = platformStatusFromApply(
    {
      state: 'available',
      available: true,
      needs_restart: false,
      current_build_sha: 'served',
      contained_upstream_sha: 'before',
    },
    {
      state: 'restart_needed',
      needs_restart: true,
      upstream_commit: 'applied',
    },
  )

  assert.equal(projected.available, false)
  assert.equal(projected.needs_restart, true)
  assert.equal(projected.contained_upstream_sha, 'applied')
})

test('a failed newer release does not forget an earlier staged update', () => {
  const projected = platformStatusFromApply(
    {
      state: 'restart_needed',
      available: true,
      needs_restart: true,
      current_build_sha: 'served',
      contained_upstream_sha: 'staged',
    },
    {
      state: 'rolled_back',
      needs_restart: false,
      upstream_commit: 'newer',
    },
  )

  assert.equal(projected.available, true)
  assert.equal(projected.needs_restart, true)
  assert.equal(projected.contained_upstream_sha, 'staged')
})

test('a conflicting newer release also preserves an earlier staged restart', () => {
  const projected = platformStatusFromApply(
    {
      state: 'restart_needed',
      available: true,
      needs_restart: true,
      current_build_sha: 'served',
      contained_upstream_sha: 'staged',
    },
    {
      state: 'conflict',
      needs_restart: false,
      upstream_commit: 'newer',
      conflict_paths: ['frontend/src/example.js'],
      chat_id: 'resolver-chat',
    },
  )

  assert.equal(projected.available, false)
  assert.equal(projected.needs_restart, true)
  assert.equal(projected.contained_upstream_sha, 'staged')
  assert.deepEqual(projected.conflict_paths, ['frontend/src/example.js'])
  assert.equal(projected.conflict_chat_id, 'resolver-chat')
})

test('update-row copy represents restart and availability independently', () => {
  assert.equal(
    platformUpdateStatusLabel({
      state: 'restart_needed',
      needs_restart: true,
      available: false,
    }),
    'Ready to restart',
  )
  assert.equal(
    platformUpdateStatusLabel({
      state: 'restart_needed',
      needs_restart: true,
      available: true,
    }),
    'More updates available',
  )
  assert.equal(
    platformUpdateStatusLabel({
      state: 'available',
      needs_restart: false,
      available: true,
    }),
    'New update available',
  )
})

test('blocking and repair states keep priority over batching copy', () => {
  assert.equal(
    platformUpdateStatusLabel({
      state: 'conflict',
      needs_restart: true,
      available: true,
    }),
    'Update blocked',
  )
  assert.equal(
    platformUpdateStatusLabel({
      state: 'rolled_back',
      needs_restart: true,
      available: true,
    }),
    'Update needs repair',
  )
})
