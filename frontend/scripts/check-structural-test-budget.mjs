import { readFile, readdir } from 'node:fs/promises'
import { dirname, join, relative } from 'node:path'
import { fileURLToPath } from 'node:url'


const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), '..')
const sourceRoot = join(frontendRoot, 'src')
const budgetPath = join(frontendRoot, 'structural-test-budget.json')
const testCall = /^\s*(?:test|it)(?:\.[A-Za-z]+)?\s*\(/gm


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
      /from\s+['"]node:fs['"]/.test(source)
      && /\breadFile(?:Sync)?\s*\(/.test(source)
    )
    if (!readsSource) continue
    inventory.push({
      path: relative(frontendRoot, path),
      cases: [...source.matchAll(testCall)].length,
    })
  }
  return inventory
}


const budget = JSON.parse(await readFile(budgetPath, 'utf8'))
const inventory = await structuralInventory()
const cases = inventory.reduce((sum, item) => sum + item.cases, 0)
const failures = []
if (inventory.length > budget.maximum_files) {
  failures.push(`${inventory.length} source-reading files exceeds ${budget.maximum_files}`)
}
if (cases > budget.maximum_cases) {
  failures.push(`${cases} source-reading cases exceeds ${budget.maximum_cases}`)
}

if (failures.length) {
  console.error('Structural-test debt grew:')
  for (const failure of failures) console.error(`  - ${failure}`)
  console.error('Replace implementation-text assertions with behavioral coverage; do not raise the budget.')
  process.exitCode = 1
} else {
  console.log(
    `Structural-test ratchet: ${inventory.length}/${budget.maximum_files} files, ${cases}/${budget.maximum_cases} cases`,
  )
}
