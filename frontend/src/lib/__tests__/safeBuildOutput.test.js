import assert from 'node:assert/strict'
import path from 'node:path'
import test from 'node:test'

import { resolveBuildOutput } from '../../../scripts/build-output-policy.mjs'


test('a live watcher lease isolates validation from the served shell', () => {
  const plan = resolveBuildOutput({
    frontendDir: '/data/platform/frontend',
    servedShellPresent: true,
    watcherLeaseHeld: true,
    tempDir: '/tmp/mobius-frontend-build-123',
  })

  assert.deepEqual(plan, {
    outDir: path.resolve('/tmp/mobius-frontend-build-123'),
    transient: true,
    reason: 'live watcher lease present',
  })
  assert.notEqual(plan.outDir, '/data/platform/frontend/dist')
})


test('a standalone checkout retains the distributable dist contract', () => {
  const plan = resolveBuildOutput({
    frontendDir: '/workspace/frontend',
    servedShellPresent: false,
    watcherLeaseHeld: false,
    tempDir: null,
  })

  assert.deepEqual(plan, {
    outDir: path.resolve('/workspace/frontend/dist'),
    transient: false,
    reason: 'standalone checkout',
  })
})


test('a copied lock cannot divert a clean image build without a served shell', () => {
  const plan = resolveBuildOutput({
    frontendDir: '/build',
    servedShellPresent: false,
    watcherLeaseHeld: true,
    tempDir: '/tmp/mobius-frontend-build-container',
  })

  assert.deepEqual(plan, {
    outDir: path.resolve('/build/dist'),
    transient: false,
    reason: 'standalone checkout',
  })
})
