#!/usr/bin/env node

import { spawnSync } from 'node:child_process'
import { readdirSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { E2E_SHARDS, projectShard } from '../tests/e2e-shards.mjs'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const assigned = E2E_SHARDS.flat()
const duplicates = assigned.filter((file, index) => assigned.indexOf(file) !== index)
const discovered = readdirSync(resolve(root, 'tests'))
  .filter(file => file.endsWith('.spec.mjs'))
  .sort()
const missing = discovered.filter(file => !assigned.includes(file))
const absent = assigned.filter(file => !discovered.includes(file))

if (duplicates.length || missing.length || absent.length) {
  throw new Error([
    duplicates.length ? `duplicate assignments: ${[...new Set(duplicates)].join(', ')}` : '',
    missing.length ? `unassigned specs: ${missing.join(', ')}` : '',
    absent.length ? `missing assigned specs: ${absent.join(', ')}` : '',
  ].filter(Boolean).join('; '))
}

const cli = resolve(root, 'node_modules', '@playwright', 'test', 'cli.js')
const listed = spawnSync(process.execPath, [cli, 'test', '--list', '--reporter=json'], {
  cwd: root,
  env: process.env,
  encoding: 'utf8',
  maxBuffer: 32 * 1024 * 1024,
})
if (listed.status !== 0) {
  process.stderr.write(listed.stderr)
  process.stderr.write(listed.stdout)
  throw new Error(`Playwright test discovery exited ${listed.status}`)
}

const report = JSON.parse(listed.stdout)
const weights = [0, 0, 0, 0]

function visit(value) {
  if (!value || typeof value !== 'object') return
  if (Array.isArray(value)) {
    for (const item of value) visit(item)
    return
  }
  if (Array.isArray(value.tests)) {
    for (const test of value.tests) {
      const shard = projectShard(test.projectName || '')
      if (shard) weights[shard - 1] += 1
    }
  }
  if (Array.isArray(value.suites)) visit(value.suites)
  if (Array.isArray(value.specs)) visit(value.specs)
}
visit(report.suites)

if (weights.some(weight => weight === 0)) {
  throw new Error(`Every shard must discover tests; got ${weights.join(':')}`)
}
// Playwright 1.62 uses these weights to set exact contiguous shard boundaries.
// Each explicit bucket is one consecutive project (shard 4 has adjacent
// ordinary/unauthenticated/timing projects), so native --shard=x/4 selects the
// intended file bucket instead of rebalancing files by raw test count.
console.log(`weights=${weights.join(':')}`)
console.log(`counts=${weights.join(',')}`)
