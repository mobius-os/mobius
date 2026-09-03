import assert from 'node:assert/strict'
import test from 'node:test'
import {
  deploymentKind,
  deploymentKindLabel,
  platformActivationLabel,
  platformStatusFromApply,
  platformStatusUnavailable,
  platformUpdateStatusLabel,
  reviewedUpdateUsesContainerRebuild,
} from '../platformUpdateState.js'

test('an unavailable release check cannot inherit a cached current claim', () => {
  const unavailable = platformStatusUnavailable({
    state: 'up_to_date',
    available: true,
    contained_upstream_sha: 'a'.repeat(40),
  })

  assert.equal(unavailable.state, 'unavailable')
  assert.equal(unavailable.available, false)
  assert.equal(unavailable.status_unavailable, true)
  assert.equal(
    platformUpdateStatusLabel(unavailable), 'Update status unavailable',
  )
})

test('the deployment classifier stays as binary as the backend that emits it', () => {
  assert.equal(deploymentKind({ deployment: 'railway' }), 'railway')
  assert.equal(deploymentKind({ deployment: 'self_hosted' }), 'self_hosted')
  // deployment_kind() returns only those two, so anything else is unknown
  // rather than a third kind the UI may render guidance for.
  for (const unknown of [undefined, null, {}, { deployment: null }, { deployment: 'fly' }]) {
    assert.equal(deploymentKind(unknown), null)
  }
})

test('the deployment badge names an unresolved deployment self-hosted', () => {
  assert.equal(deploymentKindLabel({ deployment: 'railway' }), 'Railway')
  assert.equal(deploymentKindLabel({ deployment: 'self_hosted' }), 'Self-hosted')
  assert.equal(deploymentKindLabel(null), 'Self-hosted')
})

test('only reviewed Railway image updates rebuild directly', () => {
  assert.equal(reviewedUpdateUsesContainerRebuild({
    activation: { deployment: 'railway', level: 'image_rebuild' },
  }), true)
  assert.equal(reviewedUpdateUsesContainerRebuild({
    activation: { deployment: 'self_hosted', level: 'image_rebuild' },
  }), false)
  assert.equal(reviewedUpdateUsesContainerRebuild({
    activation: { deployment: 'railway', level: 'server_restart' },
  }), false)
  assert.equal(reviewedUpdateUsesContainerRebuild(null), false)
})

test('a clean apply consumes the reviewed target but preserves restart readiness', () => {
  const projected = platformStatusFromApply(
    {
      state: 'available',
      available: true,
      status_unavailable: true,
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
  assert.equal(projected.status_unavailable, false)
  assert.equal(projected.needs_restart, true)
  assert.equal(projected.contained_upstream_sha, 'applied')
})

test('an image-required apply projects the external activation contract', () => {
  const activation = {
    level: 'image_rebuild',
    guidance: ['Rebuild and deploy.'],
  }
  const projected = platformStatusFromApply(
    { state: 'available', available: true, needs_restart: false },
    {
      state: 'activation_needed',
      needs_restart: false,
      activation,
      upstream_commit: 'applied',
    },
  )

  assert.equal(projected.available, false)
  assert.equal(projected.needs_restart, false)
  assert.equal(projected.activation, activation)
  assert.equal(platformActivationLabel(projected.activation), 'Image rebuild')
  assert.equal(platformUpdateStatusLabel(projected), 'Image rebuild required')
})

test('a dependency apply projects an in-place restart, not a rebuild', () => {
  const activation = {
    level: 'dependency_sync',
    guidance: [
      'Apply installs the new Python dependencies in place, then restart.',
    ],
  }
  const projected = platformStatusFromApply(
    { state: 'available', available: true, needs_restart: false },
    {
      state: 'restart_needed',
      needs_restart: true,
      activation,
      upstream_commit: 'applied',
    },
  )

  assert.equal(projected.available, false)
  assert.equal(projected.needs_restart, true)
  assert.equal(projected.activation, activation)
  assert.equal(
    platformActivationLabel(projected.activation), 'Dependency update',
  )
  assert.equal(platformUpdateStatusLabel(projected), 'Ready to restart')
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
  assert.equal(
    platformUpdateStatusLabel({
      state: 'activation_needed',
      activation: { level: 'proxy_reload' },
      available: false,
    }),
    'Proxy reload required',
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

test('a legacy deployment flag does not hide an available in-app update', () => {
  assert.equal(
    platformUpdateStatusLabel({
      state: 'available',
      available: true,
      updates_disabled: true,
    }),
    'New update available',
  )
})
