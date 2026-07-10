// putJSON is the single seam every save in the app flows through (the question
// card and the settings form both call storage.putJSON). These lock its HONEST
// contract on top of window.mobius.durableWrite:
//   - a server FATAL refusal (DurableWriteError, e.g. 413 quota) REJECTS, so the
//     call site's catch fires and the UI shows an error — never "Saved" over a
//     write the server threw away (the bug the old answersLanded re-read existed
//     to catch; durableWrite makes the re-read obsolete);
//   - a 'synced' resolve = server accepted = "Saved";
//   - a 'queued' resolve = durably outboxed offline (guaranteed retry) = durable
//     SUCCESS, NOT an error (the old putJSON wrongly threw on queued).
// We mock window.mobius.durableWrite so the test pins the call-site DECISION
// (resolve vs reject) without a browser or a real outbox.

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
const buildDir = join(root, '.build-dw')
const bundled = join(buildDir, 'index.mjs')

async function bundle() {
  await rm(buildDir, { recursive: true, force: true })
  await mkdir(buildDir, { recursive: true })
  await execFileAsync(esbuildPath, [
    join(root, '..', 'index.jsx'),
    '--bundle',
    '--format=esm',
    '--platform=node',
    '--jsx=automatic',
    `--outfile=${bundled}`,
  ], { env: buildEnv() })
  return import(pathToFileURL(bundled))
}

// A faithful stand-in for the runtime's DurableWriteError: a fatal refusal the
// app catches via `catch` (it never branches on `instanceof`, treating any
// throw as a save error — see the call-site comments).
class FakeDurableWriteError extends Error {
  constructor(message, fields = {}) {
    super(message)
    this.name = 'DurableWriteError'
    this.code = fields.code || 'dead_letter'
    this.status = fields.status
    this.path = fields.path
    this.retryable = fields.retryable === true
  }
}

// Install a window.mobius whose durableWrite is driven by `impl(path, obj)`.
// Records every write so a test can assert the payload reached the primitive.
function installMobius(impl) {
  const calls = []
  globalThis.window = {
    mobius: {
      durableWrite: async (path, obj) => {
        calls.push({ path, obj })
        return impl(path, obj)
      },
    },
  }
  return calls
}

test('putJSON rejects when durableWrite fatally refuses (413) - the call site shows an error, not "Saved"', async () => {
  const { makeStorage } = await bundle()
  installMobius(() => {
    throw new FakeDurableWriteError('refused (413)', { code: 'dead_letter', status: 413, path: 'x', retryable: false })
  })
  const storage = makeStorage('reflection', 'tok')

  // The app wraps putJSON in try/catch and only flips to "Saved" on a non-throw.
  // A rejecting putJSON therefore lands in catch -> error UI. We assert the
  // throw itself (the honest signal); the call-site copy is the UI consequence.
  await assert.rejects(
    () => storage.putJSON('question-answers/2026-06-22.json', { report_date: '2026-06-22', answers: { Q: 'A' } }),
    (err) => err.name === 'DurableWriteError' && err.status === 413,
  )
})

test('putJSON resolves on a synced write - the call site shows "Saved"', async () => {
  const { makeStorage } = await bundle()
  const calls = installMobius(() => ({ durability: 'synced', path: 'p', writeId: 'w1' }))
  const storage = makeStorage('reflection', 'tok')

  const res = await storage.putJSON('settings.json', { cron: '0 6 * * *' })
  assert.equal(res.durability, 'synced')
  // The payload reached the primitive unwrapped (bare object on a .json path).
  assert.equal(calls.length, 1)
  assert.deepEqual(calls[0].obj, { cron: '0 6 * * *' })
})

test('putJSON treats a queued (offline) write as durable success - it does NOT throw', async () => {
  const { makeStorage } = await bundle()
  installMobius(() => ({ durability: 'queued', path: 'p', writeId: 'w2' }))
  const storage = makeStorage('reflection', 'tok')

  // The old putJSON threw on queued (forcing an "offline" error); the migrated
  // one must resolve, because a queued write is durably outboxed with a
  // guaranteed retry. A later fatal refusal is surfaced out-of-band by
  // onDeadLetter, not by turning this success into an error.
  const res = await storage.putJSON('question-answers/2026-06-22.json', { report_date: '2026-06-22', answers: { Q: 'A' } })
  assert.equal(res.durability, 'queued')
})

test('a "superseded"/"conflict" DurableWriteError also rejects putJSON (any fatal reject -> error, never "Saved")', async () => {
  const { makeStorage } = await bundle()
  installMobius(() => {
    throw new FakeDurableWriteError('superseded', { code: 'superseded', path: 'p', retryable: false })
  })
  const storage = makeStorage('reflection', 'tok')
  // The app never passes opts.ifMatch (so 412/conflict can't occur in practice),
  // but if durableWrite rejects for ANY reason, the call site must not claim
  // "Saved" - it catches the throw and surfaces an error. Lock that putJSON
  // does not swallow a non-dead_letter reject.
  await assert.rejects(
    () => storage.putJSON('settings.json', { cron: '0 6 * * *' }),
    (err) => err.name === 'DurableWriteError' && err.code === 'superseded',
  )
})
