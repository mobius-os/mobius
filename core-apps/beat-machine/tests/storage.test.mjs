import assert from 'node:assert/strict'
import { mkdir, rm } from 'node:fs/promises'
import { dirname, join } from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import test from 'node:test'
import { buildEnv, esbuildPath } from './test-deps.mjs'

const execFileAsync = promisify(execFile)
const root = dirname(fileURLToPath(import.meta.url))
const buildDir = join(root, '.build')
const bundled = join(buildDir, 'storage.mjs')

async function bundle() {
  await rm(buildDir, { recursive: true, force: true })
  await mkdir(buildDir, { recursive: true })
  await execFileAsync(esbuildPath, [
    join(root, '..', 'storage.js'),
    '--bundle',
    '--format=esm',
    '--platform=node',
    `--alias:react=${join(root, 'fixtures', 'react-stub.mjs')}`,
    `--outfile=${bundled}`,
  ], { env: buildEnv() })
  return import(pathToFileURL(bundled))
}

test('loadBeatState returns defaults for a real missing state file', async () => {
  const oldFetch = globalThis.fetch
  globalThis.window = {}
  globalThis.fetch = async () => new Response('', { status: 404 })
  try {
    const { loadBeatState } = await bundle()
    const state = await loadBeatState('beat-machine', 'tok')
    assert.equal(state.bpm, 120)
    assert.equal(state.grid.length, 16)
    assert.equal(state.grid[0].length, 32)
  } finally {
    globalThis.fetch = oldFetch
    delete globalThis.window
  }
})

test('loadBeatState rejects transient storage failures instead of returning empty state', async () => {
  const oldFetch = globalThis.fetch
  globalThis.window = {}
  globalThis.fetch = async () => new Response('temporarily unavailable', { status: 503 })
  try {
    const { loadBeatState } = await bundle()
    await assert.rejects(
      () => loadBeatState('beat-machine', 'tok'),
      /GET state\.json failed \(503\)/,
    )
  } finally {
    globalThis.fetch = oldFetch
    delete globalThis.window
  }
})

test('loadBeatState propagates runtime bridge failures', async () => {
  globalThis.window = {
    mobius: {
      storage: {
        get: async () => {
          throw new Error('offline mirror unavailable')
        },
      },
    },
  }
  try {
    const { loadBeatState } = await bundle()
    await assert.rejects(
      () => loadBeatState('beat-machine', 'tok'),
      /offline mirror unavailable/,
    )
  } finally {
    delete globalThis.window
  }
})
