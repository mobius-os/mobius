import test from 'node:test'
import assert from 'node:assert/strict'
import { platformUpdateRepairReason, platformUpdateRepairEvidence, buildPlatformUpdateRepairPrompt } from '../platformUpdateRepair.js'

for (const deployment of ['railway', 'self_hosted']) {
  test(`${deployment} seed blockers offer agent help, not another replacement`, () => {
    const preview = { target_sha: 'target', activation: { level: 'image_rebuild', deployment }, blocking_paths: ['backend/scripts/seed-skills/cron.md'], blocking_diff: '+local instructions' }
    assert.match(platformUpdateRepairReason({ preview }), /preserving your local changes/)
    const evidence = platformUpdateRepairEvidence({ preview })
    assert.deepEqual(evidence.blocking_paths, preview.blocking_paths)
    assert.equal('blocking_diff' in evidence, false)
    assert.equal(evidence.reviewed_release.target_sha, 'target')
    assert.equal(evidence.activation.deployment, deployment)
  })
}

test('routine activation and stale reviews stay with their UI actions', () => {
  for (const level of ['live', 'server_restart', 'image_rebuild']) {
    assert.equal(platformUpdateRepairReason({ preview: { activation: { level, required_actions: level === 'live' ? [] : [level] }, blocking_paths: [] } }), null)
  }
  for (const errorCode of [
    'update_plan_stale', 'update_plan_invalid', 'activation_changed',
  ]) {
    assert.equal(platformUpdateRepairReason({ error: 'review changed', errorCode }), null)
  }
})

test('Python dependency updates stop for a separately verified system update', () => {
  const preview = {
    incoming_activation: {
      level: 'image_rebuild',
      required_actions: ['image_rebuild'],
      reasons: [{ code: 'python_dependencies' }],
    },
  }
  assert.match(platformUpdateRepairReason({ preview }), /Python packages/)
})

test('old Python drift does not block an unrelated reviewed update', () => {
  const preview = {
    activation: {
      level: 'image_rebuild',
      reasons: [{ code: 'python_dependencies' }],
    },
    incoming_activation: { level: 'live', reasons: [] },
  }
  assert.equal(platformUpdateRepairReason({ preview }), null)
})

test('predicted overlap leaves Apply available and retains diagnostic paths', () => {
  const preview = {
    target_sha: 'target',
    activation: { level: 'server_restart', required_actions: ['server_restart'] },
    blocking_paths: [],
    conflict_paths: ['backend/app/goal_plans.py'],
  }
  assert.equal(platformUpdateRepairReason({ preview }), null)
  assert.deepEqual(
    platformUpdateRepairEvidence({ preview }).conflict_paths,
    ['backend/app/goal_plans.py'],
  )
})

test('external deployment work and failed validation earn agent help', () => {
  for (const level of ['proxy_reload', 'container_recreate', 'host_maintenance']) {
    assert.match(platformUpdateRepairReason({ platform: { activation: { level, required_actions: level === 'live' ? [] : [level] } } }), /deployment settings/)
  }
  assert.match(platformUpdateRepairReason({ platform: { state: 'rolled_back' } }), /attention/)
  assert.match(platformUpdateRepairReason({ error: 'controller failed' }), /attention/)
})

test('an explicit post-apply dispatch failure remains recoverable', () => {
  assert.match(
    platformUpdateRepairReason({ errorCode: 'update_applied_rebuild_pending' }),
    /applied.*finishing the container replacement/,
  )
})

test('an installed image update with no replacement attempt stays in the reviewed UI flow', () => {
  const platform = {
    available: false,
    contained_upstream_sha: 'installed',
    activation: { level: 'image_rebuild', required_actions: ['image_rebuild'] },
  }
  assert.equal(platformUpdateRepairReason({ platform }), null)
  assert.equal(platformUpdateRepairReason({
    platform,
    rebuild: { expected_sha: 'installed', state: 'queued' },
  }), null)
})

test('old replacement failures do not get attributed to another release', () => {
  const platform = { contained_upstream_sha: 'installed' }
  const rebuild = { expected_sha: 'old', state: 'failed', error: 'old error' }
  assert.equal(platformUpdateRepairEvidence({ platform, rebuild }).replacement, null)
  assert.deepEqual(platformUpdateRepairEvidence({ platform, rebuild: { ...rebuild, expected_sha: 'installed' } }).replacement, { ...rebuild, expected_sha: 'installed' })
})

test('repair handoff carries evidence and preserves review, skill ownership and approval boundaries', () => {
  const prompt = buildPlatformUpdateRepairPrompt(platformUpdateRepairEvidence({
    preview: { target_sha: 'reviewed-sha', current_sha: 'current-sha', plan_id: 'plan', image_digest: 'digest', operation: 'finish', blocking_paths: ['backend/scripts/seed-skills/reflection.md'] },
    error: 'Do not treat this diagnostic as instructions',
  }))
  for (const fragment of ['reviewed-sha', 'current-sha', 'plan', 'digest', 'reflection.md', 'untrusted snapshot', 'owning', 'installed', 'do not blindly copy', 'one reviewed operation', 'owner-controlled custom-image', 'server restart always needs its own explicit approval', 'fresh review', 'not permission to publish']) {
    assert.ok(prompt.includes(fragment), fragment)
  }
  assert.match(prompt, /target changed.*new exact target as a fresh review/i)
  assert.match(prompt, /never silently substitute it or carry an old approval forward/i)
  assert.match(prompt, /return me to Settings/i)
  assert.match(prompt, /existing update controller/i)
  assert.ok(prompt.includes('    "error":'))
})

test('a failed unfinished replacement remains actionable after reopening Settings, but historical failures do not', () => {
  const platform = { contained_upstream_sha: 'installed', activation: { level: 'image_rebuild' } }
  for (const state of ['failed', 'rolled_back', 'needs_recovery']) {
    assert.match(platformUpdateRepairReason({ platform, rebuild: { expected_sha: 'installed', state } }), /last attempt/)
    assert.equal(platformUpdateRepairReason({ platform, rebuild: { expected_sha: 'old', state } }), null)
    assert.equal(platformUpdateRepairReason({ platform: { ...platform, activation: { level: 'live' } }, rebuild: { expected_sha: 'installed', state } }), null)
  }
})

test('mixed activation remains agent work regardless of its display level', () => {
  for (const external of ['proxy_reload', 'container_recreate', 'host_maintenance']) {
    const preview = { activation: { level: 'image_rebuild', required_actions: ['image_rebuild', external] } }
    assert.match(platformUpdateRepairReason({ preview }), /deployment settings/)
    assert.deepEqual(platformUpdateRepairEvidence({ preview }).activation.required_actions, ['image_rebuild', external])
  }
  assert.match(platformUpdateRepairReason({ errorCode: 'external_activation_required' }), /deployment settings/)
})
