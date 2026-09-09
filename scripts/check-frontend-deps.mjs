#!/usr/bin/env node

import fs from 'node:fs'
import path from 'node:path'
import { spawnSync } from 'node:child_process'

const [frontendArg] = process.argv.slice(2)

function finish(status, code) {
  process.stdout.write(`${status}\n`)
  process.exit(code)
}

if (!frontendArg) finish('incomplete', 4)

const frontend = path.resolve(frontendArg)
const modules = path.join(frontend, 'node_modules')
if (!fs.existsSync(modules)) finish('missing', 2)
let npmValidationRoot = frontend

let lock
let manifest
try {
  lock = JSON.parse(fs.readFileSync(path.join(frontend, 'package-lock.json'), 'utf8'))
  manifest = JSON.parse(fs.readFileSync(path.join(frontend, 'package.json'), 'utf8'))
} catch {
  finish('lock-mismatch', 3)
}

const packages = lock?.packages
if (!packages || typeof packages !== 'object') finish('lock-mismatch', 3)

function stableJson(value) {
  if (Array.isArray(value)) return value.map(stableJson)
  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value).sort(([left], [right]) => left.localeCompare(right))
        .map(([key, child]) => [key, stableJson(child)]),
    )
  }
  return value
}

function sameJson(left, right) {
  return JSON.stringify(stableJson(left)) === JSON.stringify(stableJson(right))
}

// npm ls must run at a symlinked install's canonical root, where it validates
// the canonical package.json. Independently prove that the review manifest is
// represented by its own lock root before relying on that canonical result.
// This also rejects a common bad review state: package.json changed without a
// corresponding npm install/package-lock update.
const lockRoot = packages['']
if (!lockRoot || typeof lockRoot !== 'object') finish('lock-mismatch', 3)
for (const field of [
  'dependencies',
  'devDependencies',
  'optionalDependencies',
  'peerDependencies',
  'peerDependenciesMeta',
]) {
  const manifestValue = manifest?.[field] ?? {}
  const lockValue = lockRoot[field] ?? {}
  if (
    !manifestValue
    || typeof manifestValue !== 'object'
    || Array.isArray(manifestValue)
    || !lockValue
    || typeof lockValue !== 'object'
    || Array.isArray(lockValue)
    || !sameJson(manifestValue, lockValue)
  ) finish('lock-mismatch', 3)
}

try {
  if (fs.lstatSync(modules).isSymbolicLink()) {
    // npm canonicalizes the linked package directories. Running `npm ls` from
    // the borrowing worktree then reports the canonical packages as
    // extraneous even though this lock was already proven byte-identical by
    // frontend-deps.sh. Validate completeness at the install's real package
    // root; the recursive version checks below still use the review lock.
    npmValidationRoot = path.dirname(fs.realpathSync(modules))
    const canonicalManifest = JSON.parse(
      fs.readFileSync(path.join(npmValidationRoot, 'package.json'), 'utf8'),
    )
    // npm lock roots do not reliably preserve these install-affecting fields.
    // Borrow only when the reviewed and canonical manifests agree. Unknown
    // future workspace layouts are conservatively rejected by this equality.
    for (const field of ['overrides', 'workspaces']) {
      if (!sameJson(manifest?.[field] ?? null, canonicalManifest?.[field] ?? null)) {
        finish('lock-mismatch', 3)
      }
    }
  }
} catch {
  finish('incomplete', 4)
}

const visited = new Set()
let mismatch = false
let incomplete = false

function inspectPackage(packagePath, lockKey) {
  let installedPath
  let manifest
  try {
    installedPath = fs.realpathSync(packagePath)
    manifest = JSON.parse(fs.readFileSync(path.join(packagePath, 'package.json'), 'utf8'))
  } catch {
    incomplete = true
    return
  }
  const expected = packages[lockKey]
  if (!expected || typeof expected !== 'object') {
    mismatch = true
    return
  }
  if (expected.link === true) {
    try {
      const expectedTarget = typeof expected.resolved === 'string'
        ? fs.realpathSync(path.resolve(frontend, expected.resolved))
        : ''
      if (!expectedTarget || expectedTarget !== installedPath) mismatch = true
    } catch {
      mismatch = true
    }
  } else if (
    typeof expected.version !== 'string'
    || manifest.version !== expected.version
  ) {
    mismatch = true
  }
  if (visited.has(installedPath)) return
  visited.add(installedPath)
  inspectModules(path.join(packagePath, 'node_modules'), `${lockKey}/node_modules`)
}

function inspectModules(directory, lockPrefix) {
  let entries
  try {
    entries = fs.readdirSync(directory, { withFileTypes: true })
  } catch (error) {
    if (error?.code !== 'ENOENT') incomplete = true
    return
  }
  for (const entry of entries) {
    if (entry.name.startsWith('.')) continue
    const entryPath = path.join(directory, entry.name)
    if (entry.name.startsWith('@')) {
      let scoped
      try {
        scoped = fs.readdirSync(entryPath, { withFileTypes: true })
      } catch {
        incomplete = true
        continue
      }
      for (const child of scoped) {
        if (child.name.startsWith('.')) continue
        inspectPackage(
          path.join(entryPath, child.name),
          `${lockPrefix}/${entry.name}/${child.name}`,
        )
      }
      continue
    }
    inspectPackage(entryPath, `${lockPrefix}/${entry.name}`)
  }
}

inspectModules(modules, 'node_modules')
if (incomplete) finish('incomplete', 4)
if (mismatch) finish('lock-mismatch', 3)

// Exact installed versions are necessary but not sufficient: npm still owns
// required/optional dependency resolution and reports a missing or invalid
// edge anywhere in the tree. Keep that completeness proof after the lock
// comparison so every version drift has the more precise lock-mismatch result.
const listed = spawnSync('npm', ['ls', '--all'], {
  cwd: npmValidationRoot,
  stdio: 'ignore',
})
if (listed.status !== 0) finish('incomplete', 4)
finish('ready', 0)
