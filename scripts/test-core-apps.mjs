import { spawnSync } from 'node:child_process'
import { existsSync, readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = dirname(dirname(fileURLToPath(import.meta.url)))
const coreAppsDir = join(root, 'core-apps')
const frontendNodeModules = join(root, 'frontend', 'node_modules')

if (!existsSync(join(frontendNodeModules, '.bin', 'esbuild'))) {
  console.error('Missing frontend test dependencies. Run npm ci in frontend/.')
  process.exit(1)
}

const apps = readdirSync(coreAppsDir, { withFileTypes: true })
  .filter((entry) => entry.isDirectory())
  .map((entry) => entry.name)
  .sort()

let failed = false

for (const slug of apps) {
  const appDir = join(coreAppsDir, slug)
  const packageJson = join(appDir, 'package.json')
  if (!existsSync(packageJson)) continue

  const pkg = JSON.parse(readFileSync(packageJson, 'utf8'))
  if (!pkg.scripts?.test) continue

  console.log(`core-apps/${slug}: npm test`)
  const result = spawnSync('npm', ['test'], {
    cwd: appDir,
    env: {
      ...process.env,
      MOBIUS_FRONTEND_NODE_MODULES: frontendNodeModules,
    },
    stdio: 'inherit',
  })
  if (result.status !== 0) failed = true
}

if (failed) process.exit(1)
