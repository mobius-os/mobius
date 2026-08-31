import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'

import { enterBuildAdmission } from './build-admission.mjs'
import { resolveBuildOutput } from './build-output-policy.mjs'


enterBuildAdmission({ vite: true })

const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const watcherLeasePath = path.join(frontendDir, '.watch.lock')
const servedShellPresent = fs.existsSync(
  path.join(frontendDir, 'dist', 'index.html'),
)


function watcherLeaseIsHeld(leasePath) {
  if (!fs.existsSync(leasePath)) return false
  const result = spawnSync('flock', ['--nonblock', leasePath, 'true'], {
    stdio: 'ignore',
  })
  // A probe failure is safer to treat as a live lease than to risk emptying the
  // served shell. Normal standalone builds acquire the stale lease and return 0.
  return Boolean(result.error) || result.status !== 0
}


const watcherLeaseHeld = watcherLeaseIsHeld(watcherLeasePath)
const isolateBuild = servedShellPresent && watcherLeaseHeld
const tempDir = isolateBuild
  ? fs.mkdtempSync(path.join(os.tmpdir(), 'mobius-frontend-build-'))
  : null
const plan = resolveBuildOutput({
  frontendDir,
  servedShellPresent,
  watcherLeaseHeld,
  tempDir,
})
const childEnv = { ...process.env }
if (!/--max[-_]old[-_]space[-_]size(?:=|\s)/.test(childEnv.NODE_OPTIONS || '')) {
  childEnv.NODE_OPTIONS = `${childEnv.NODE_OPTIONS || ''} --max-old-space-size=384`.trim()
}


function run(command, args) {
  const result = spawnSync(command, args, {
    cwd: frontendDir,
    env: childEnv,
    stdio: 'inherit',
  })
  if (result.error) throw result.error
  if (result.signal) {
    throw new Error(`${path.basename(command)} exited on ${result.signal}`)
  }
  if (result.status !== 0) {
    const error = new Error(
      `${path.basename(command)} exited with status ${result.status ?? 1}`,
    )
    error.exitCode = result.status ?? 1
    throw error
  }
}


try {
  if (plan.transient) {
    console.log(
      'Live shell detected: validating in an isolated directory; '
      + 'the frontend watcher remains the only publisher.',
    )
  }
  run(process.execPath, [
    path.join(frontendDir, 'scripts', 'check-speech-pitch-asset.mjs'),
  ])
  run(process.execPath, [
    path.join(frontendDir, 'scripts', 'build-runtime.mjs'),
  ])
  run(process.execPath, [
    path.join(frontendDir, 'node_modules', 'vite', 'bin', 'vite.js'),
    'build',
    '--configLoader',
    'runner',
    '--outDir',
    plan.outDir,
    '--emptyOutDir',
  ])
  run(process.execPath, [
    path.join(frontendDir, 'scripts', 'check-built-globals.mjs'),
    plan.outDir,
  ])
  run(process.execPath, [
    path.join(frontendDir, 'scripts', 'check-offline-build.mjs'),
    plan.outDir,
  ])
} catch (error) {
  if (!Number.isInteger(error?.exitCode)) console.error(error)
  process.exitCode = error?.exitCode || 1
} finally {
  if (plan.transient) fs.rmSync(plan.outDir, { recursive: true, force: true })
}
