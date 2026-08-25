import { readFile, readdir } from 'node:fs/promises'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'

const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), '..')
const sourceRoot = join(frontendRoot, 'src')
const allowlistPath = join(frontendRoot, 'structural-test-allowlist.json')

async function testFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true })
  const nested = await Promise.all(entries.map(async entry => {
    const path = join(directory, entry.name)
    if (entry.isDirectory()) return testFiles(path)
    return /\.test\.[cm]?[jt]sx?$/.test(entry.name) ? [path] : []
  }))
  return nested.flat()
}

async function structuralInventory() {
  const inventory = []
  for (const path of await testFiles(sourceRoot)) {
    const source = await readFile(path, 'utf8')
    const readsSource = (
      /from\s+['"]node:fs(?:\/promises)?['"]/.test(source)
      && /\breadFile(?:Sync)?\b/.test(source)
    )
    if (!readsSource) continue
    inventory.push(relative(frontendRoot, path))
  }
  return inventory.sort()
}

const allowlist = JSON.parse(await readFile(allowlistPath, 'utf8'))
const inventory = await structuralInventory()
const allowed = new Set(allowlist.allowed_files || [])
const actual = new Set(inventory)
const failures = []
for (const path of inventory) {
  if (!allowed.has(path)) failures.push(`new source-reading test: ${path}`)
}
for (const path of allowed) {
  if (!actual.has(path)) failures.push(`obsolete allowlist entry: ${path}`)
}

if (failures.length) {
  console.error('Structural-test migration list is stale:')
  for (const failure of failures) console.error(`  - ${failure}`)
  console.error(
    'Replace new implementation-text assertions with behavioral coverage. '
    + 'When a listed file becomes behavioral, remove its allowlist entry.',
  )
  process.exitCode = 1
} else {
  console.log(
    `Structural-test migration allowlist: ${inventory.length} known files; new files forbidden`,
  )
}
