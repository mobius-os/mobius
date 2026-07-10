#!/usr/bin/env node
import { readdirSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { spawnSync } from 'node:child_process'

const [loader, ...roots] = process.argv.slice(2)

if (!loader || roots.length === 0) {
  console.error('usage: run-node-tests.mjs <loader> <root> [root...]')
  process.exit(2)
}

function collectTests(path, out = []) {
  const stat = statSync(path)
  if (stat.isDirectory()) {
    for (const entry of readdirSync(path).sort()) {
      collectTests(join(path, entry), out)
    }
    return out
  }
  if (path.endsWith('.test.js')) out.push(path)
  return out
}

const files = roots.flatMap(root => collectTests(root)).sort()

if (files.length === 0) {
  console.error(`no test files found under: ${roots.join(', ')}`)
  process.exit(1)
}

const result = spawnSync(
  process.execPath,
  ['--loader', loader, '--test', ...files],
  { stdio: 'inherit' },
)

if (result.error) {
  console.error(result.error)
  process.exit(1)
}

process.exit(result.status ?? 1)
