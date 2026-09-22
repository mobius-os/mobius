import fs from 'node:fs'
import path from 'node:path'
import { spawnSync } from 'node:child_process'
import { fileURLToPath } from 'node:url'


const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const backendDir = path.resolve(frontendDir, '..', 'backend')
const admissionModule = path.join(backendDir, 'app', 'build_admission.py')
const activeBackendEnv = '_MOBIUS_BUILD_ADMISSION_BACKEND'


export function enterBuildAdmission() {
  // The image's frontend-only build stage and standalone frontend checkouts do
  // not contain a sibling backend. Runtime checkouts do, and must use its one
  // authoritative flock rather than duplicating it here.
  if (!fs.existsSync(admissionModule)) return

  const activeBackend = fs.realpathSync(backendDir)
  // This private marker only prevents a child build from flocking behind its
  // own parent. The exact backend path avoids suppressing admission for a
  // different checkout; it is process coordination, not a security boundary.
  if (process.env[activeBackendEnv] === activeBackend) return

  const childEnv = {
    ...process.env,
    [activeBackendEnv]: activeBackend,
    PYTHONPATH: [activeBackend, process.env.PYTHONPATH].filter(Boolean).join(path.delimiter),
  }
  const args = ['-m', 'app.build_admission']
  args.push('--', process.execPath, ...process.execArgv, ...process.argv.slice(1))
  const result = spawnSync('python3', args, {
    cwd: process.cwd(),
    env: childEnv,
    stdio: 'inherit',
  })
  if (result.error) throw result.error
  if (result.signal) process.kill(process.pid, result.signal)
  process.exit(result.status ?? 1)
}
