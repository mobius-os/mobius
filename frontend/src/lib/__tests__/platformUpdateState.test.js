import assert from 'node:assert/strict'
import test from 'node:test'
import {
  deploymentKind,
  deploymentKindLabel,
  platformActivationLabel,
  platformStatusFromApply,
  platformPackageWarning,
  platformStatusUnavailable,
  platformUpdateStall,
  platformUpdateStatusLabel,
  reviewedUpdateUsesContainerRebuild,
} from '../platformUpdateState.js'

test('image-only recovery does not announce a completed full update', () => {
  assert.equal(platformUpdateStatusLabel({
    state: 'available', available: true, image_build_applied: false,
  }), 'Code update required')
  assert.equal(platformUpdateStatusLabel({
    state: 'source_unconfirmed', available: false, image_build_applied: false,
  }), 'Running code not confirmed')
})

test('optimistic apply does not borrow image pairing from checkout progress', () => {
  const projected = platformStatusFromApply({ image_build_applied: false }, {
    state: 'up_to_date', upstream_commit: 'a'.repeat(40),
  })
  assert.equal(platformUpdateStatusLabel(projected), 'Running code not confirmed')
})

test('paired local merge and separately owed image activation stay independent', () => {
  assert.equal(platformUpdateStatusLabel({
    state: 'up_to_date', available: false, image_build_applied: true,
  }), 'Up to date')
  assert.equal(platformUpdateStatusLabel({
    state: 'activation_needed', image_build_applied: true,
    activation: { level: 'image_rebuild' },
  }), 'Ready to finish')
})

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

test('host maintenance tells the owner where to finish the update', () => {
  assert.equal(platformUpdateStatusLabel({
    state: 'activation_needed',
    activation: { level: 'host_maintenance' },
  }), 'Finish this update on your server')
})

test('the existing update restart takes precedence over newer releases', () => {
  assert.equal(platformUpdateStatusLabel({
    state: 'restart_needed', available: true, needs_restart: true,
    unfinished_update: { stage: 'finish', action: 'restart' },
  }), 'Restart to finish the update')
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

test('reviewed image updates rebuild directly on both deployments', () => {
  assert.equal(reviewedUpdateUsesContainerRebuild({
    activation: { deployment: 'railway', level: 'image_rebuild', required_actions: ['image_rebuild'] },
  }), true)
  // Self-hosted image updates now also drive the rebuild from the update flow.
  assert.equal(reviewedUpdateUsesContainerRebuild({
    activation: { deployment: 'self_hosted', level: 'image_rebuild', required_actions: ['image_rebuild'] },
  }), true)
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
    level: 'image_rebuild', required_actions: ['image_rebuild'],
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
  assert.equal(platformUpdateStatusLabel(projected), 'Ready to finish')
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
    'Update needs help',
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

test('routine image replacement requires complete action evidence without external work', () => {
  for (const external of ['proxy_reload', 'container_recreate', 'host_maintenance']) {
    const activation = { level: 'image_rebuild', required_actions: ['image_rebuild', external] }
    assert.equal(reviewedUpdateUsesContainerRebuild({ activation }), false)
    assert.equal(platformUpdateStatusLabel({ activation }), 'Update needs help')
  }
  assert.equal(reviewedUpdateUsesContainerRebuild({ activation: { level: 'image_rebuild' } }), false)
  assert.equal(reviewedUpdateUsesContainerRebuild({ activation: { level: 'image_rebuild', required_actions: ['server_restart', 'image_rebuild'] } }), true)
})

test('a stalled update names how long and why, with the backend\'s actions', () => {
  assert.equal(platformUpdateStall({ unfinished_update: { stage: 'resolve' } }), null)

  const idle = platformUpdateStall({
    unfinished_update: { stage: 'resolve', stalled: {
      reason: 'resolver_idle', idle_hours: 31.6, actions: ['resume', 'retry', 'cancel'],
    } },
  })
  assert.deepEqual(idle, {
    message: 'Update paused for 32 h: resolver idle.', canResume: true, canRetry: true,
  })

  const blocked = platformUpdateStall({
    restart_blocker: { packages: [{ name: 'claude-agent-sdk' }], blocking: true },
    unfinished_update: { stage: 'finish', stalled: { reason: 'restart_blocked', idle_hours: 0, actions: [] } },
  })
  assert.match(blocked.message, /cannot restart into it \(needs claude-agent-sdk\)/)
  assert.equal(blocked.canResume, false)
  assert.equal(blocked.canRetry, false)
})

test('a refused restart for a finish that needs a new container points at replacing it', () => {
  const platform = {
    restart_blocker: { packages: [{ name: 'claude-agent-sdk' }], blocking: true },
    unfinished_update: { stage: 'finish', action: 'replace', stalled: {
      reason: 'restart_blocked', idle_hours: 0, actions: [],
    } },
  }
  assert.equal(
    platformUpdateStall(platform).message,
    'Update paused: it needs a new container (needs claude-agent-sdk). Choose Finish update to replace this one.',
  )
  // The replacement under way is the progress: nothing is paused.
  assert.equal(platformUpdateStall(platform, { state: 'replacing' }), null)
  assert.notEqual(platformUpdateStall(platform, { state: 'succeeded' }), null)
})

test('packages behind the source warn, naming each one and the container update that fixes it', () => {
  const platform = {
    restart_blocker: {
      origin: 'official_newer', blocking: false, fix: { action: 'replace_container', target_sha: 'c'.repeat(40) },
      packages: [
        { name: 'aiohttp', declared: '==3.13.0', installed: '3.12.1', kind: 'version', origin: 'official_newer' },
        { name: 'pillow', declared: '==13.0', installed: '12.3', kind: 'version', origin: 'official_newer' },
      ],
    },
  }
  const warning = platformPackageWarning(platform)
  assert.equal(warning.title, 'Container packages are behind the platform source')
  assert.equal(
    warning.message,
    'Möbius keeps running on the installed versions until the container is updated: '
      + 'aiohttp (source declares ==3.13.0, container has 3.12.1); pillow (source declares ==13.0, container has 12.3). '
      + 'A container update (Finish update) installs them.',
  )
  // Nothing to warn about without a gap, or while the replacement runs.
  assert.equal(platformPackageWarning({ restart_blocker: null }), null)
  assert.equal(platformPackageWarning(platform, { state: 'replacing' }), null)
  // A stall already names a refused restart's packages.
  assert.equal(platformPackageWarning({
    ...platform, unfinished_update: { stage: 'finish', stalled: { reason: 'restart_blocked' } },
  }), null)
})

test('a local package gap names the commit to revert and an unverifiable line', () => {
  const warning = platformPackageWarning({
    restart_blocker: {
      origin: 'local', blocking: false, fix: { action: 'revert_local_declaration', commit: 'abcdef1234567890' },
      packages: [
        { name: '-e ./local-package', declared: null, installed: null, kind: 'unsupported', origin: 'local' },
        { name: 'extra', declared: '', installed: null, kind: 'missing', origin: 'local' },
      ],
    },
  })
  assert.match(warning.message, /-e \.\/local-package \(a declaration Möbius cannot verify\)/)
  assert.match(warning.message, /extra \(source declares any version, container does not have it\)/)
  assert.match(warning.message, /revert local commit abcdef123456, or update the container once that change is released/)
  const uncommitted = platformPackageWarning({
    restart_blocker: { origin: 'local', blocking: false, fix: { commit: null },
      packages: [{ name: 'extra', declared: '==1', installed: null, kind: 'missing', origin: 'local' }] },
  })
  assert.match(uncommitted.message, /revert the uncommitted edit/)
})

test('a mixed gap with an uncommitted local edit still says to revert that edit', () => {
  const warning = platformPackageWarning({
    restart_blocker: {
      origin: 'official_newer', blocking: false,
      fix: { action: 'replace_container', target_sha: 'c'.repeat(40), commit: null },
      packages: [
        { name: 'aiohttp', declared: '==3.13.0', installed: '3.12.1', kind: 'version', origin: 'official_newer' },
        { name: 'extra', declared: '==1', installed: null, kind: 'missing', origin: 'local' },
      ],
    },
  })
  assert.match(warning.message, /Some come from a local change; if the new container still lacks them, revert the uncommitted edit\./)
})
