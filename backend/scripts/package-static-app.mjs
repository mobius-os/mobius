#!/usr/bin/env node
import fs from 'node:fs'
import path from 'node:path'
import process from 'node:process'
import { fileURLToPath } from 'node:url'

const SKIP_EXTS = new Set(['.map'])
const TEXT_EXTS = new Set(['.css', '.html'])

function usage() {
  return `Usage:
  node <path-to>/package-static-app.mjs --id <slug> --name <name> --version <version> \\
    --description <text> --build-dir <build|dist> --out-dir <repo> [options]

Options:
  --homepage <url>          Manifest homepage.
  --author <name>           Manifest author.
  --license <id>            Manifest license.
  --icon <path>             Repo-relative manifest icon path.
  --entry <path>            Wrapper entry path. Default: index.jsx.
  --no-rewrite-root-refs    Do not rewrite /static/... HTML/CSS refs.
  --include-sourcemaps      Include *.map files in static_assets.
  --offline-capable         Set offline_capable true.
  --force                   Overwrite existing entry/mobius.json.

The build directory is not copied. The generated mobius.json maps every build
file into static_assets so the installer copies them below /app-assets.`
}

function parseArgs(argv) {
  const opts = {
    entry: 'index.jsx',
    rewriteRootRefs: true,
    includeSourcemaps: false,
    offlineCapable: false,
    force: false,
  }
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    if (arg === '--help' || arg === '-h') {
      opts.help = true
    } else if (arg === '--no-rewrite-root-refs') {
      opts.rewriteRootRefs = false
    } else if (arg === '--include-sourcemaps') {
      opts.includeSourcemaps = true
    } else if (arg === '--offline-capable') {
      opts.offlineCapable = true
    } else if (arg === '--force') {
      opts.force = true
    } else if (arg.startsWith('--')) {
      const key = arg.slice(2).replace(/-([a-z])/g, (_, ch) => ch.toUpperCase())
      const value = argv[i + 1]
      if (!value || value.startsWith('--')) {
        throw new Error(`${arg} requires a value.`)
      }
      opts[key] = value
      i += 1
    } else {
      throw new Error(`Unexpected argument: ${arg}`)
    }
  }
  return opts
}

function relPath(from, to) {
  let rel = path.posix.relative(path.posix.dirname(from), to)
  if (!rel.startsWith('.')) rel = `./${rel}`
  return rel
}

function toPosix(p) {
  return p.split(path.sep).join('/')
}

function validateSlug(id) {
  if (!/^[a-z0-9][a-z0-9_-]*$/.test(id) || /^\d+$/.test(id)) {
    throw new Error(
      'Manifest id must use a-z, 0-9, _, -; start with a letter/number; and not be purely numeric.',
    )
  }
}

function validateRepoPath(value, field) {
  if (!value || path.isAbsolute(value) || value.includes('\\')) {
    throw new Error(`${field} must be a repo-relative path.`)
  }
  const parts = value.split('/')
  if (parts.some(part => !part || part === '.' || part === '..')) {
    throw new Error(`${field} must not contain empty, ".", or ".." segments.`)
  }
}

function walkFiles(root, includeSourcemaps) {
  const out = []
  function walk(dir) {
    for (const ent of fs.readdirSync(dir, { withFileTypes: true })) {
      if (ent.name === '.DS_Store') continue
      const abs = path.join(dir, ent.name)
      if (ent.isDirectory()) {
        walk(abs)
      } else if (ent.isFile()) {
        if (!includeSourcemaps && SKIP_EXTS.has(path.extname(ent.name))) continue
        out.push(abs)
      }
    }
  }
  walk(root)
  return out.sort((a, b) => a.localeCompare(b))
}

function isSkippableUrl(raw) {
  const value = raw.trim()
  return (
    !value ||
    value.startsWith('#') ||
    value.startsWith('data:') ||
    value.startsWith('blob:') ||
    /^[a-z][a-z0-9+.-]*:/i.test(value) ||
    value.startsWith('//')
  )
}

function stripUrlDecorators(raw) {
  const value = raw.trim().replace(/^['"]|['"]$/g, '')
  const splitAt = value.search(/[?#]/)
  if (splitAt === -1) return { pathname: value, suffix: '' }
  return { pathname: value.slice(0, splitAt), suffix: value.slice(splitAt) }
}

function rootRelativeTarget(buildDir, pathname) {
  return path.resolve(buildDir, `.${decodeURIComponent(pathname)}`)
}

function relativeTarget(buildDir, fileAbs, pathname) {
  return path.resolve(path.dirname(fileAbs), decodeURIComponent(pathname))
}

function inBuild(buildDir, candidate) {
  const rel = path.relative(buildDir, candidate)
  return rel && !rel.startsWith('..') && !path.isAbsolute(rel)
}

function rewriteCssUrls({ buildDir, fileAbs, rel, text, warnings, errors, rewriteRootRefs }) {
  return text.replace(/url\(\s*([^)]*?)\s*\)/g, (match, rawUrl) => {
    const unquoted = rawUrl.trim().replace(/^['"]|['"]$/g, '')
    if (isSkippableUrl(unquoted)) return match
    const { pathname, suffix } = stripUrlDecorators(unquoted)
    if (!pathname) return match

    const target = pathname.startsWith('/')
      ? rootRelativeTarget(buildDir, pathname)
      : relativeTarget(buildDir, fileAbs, pathname)
    if (!inBuild(buildDir, target) || !fs.existsSync(target)) {
      errors.push(`${rel}: unresolved CSS url(${unquoted})`)
      return match
    }
    if (!pathname.startsWith('/')) return match
    if (!rewriteRootRefs) {
      errors.push(`${rel}: absolute CSS url(${unquoted}) requires --rewrite-root-refs`)
      return match
    }
    const targetRel = toPosix(path.relative(buildDir, target))
    const rewritten = relPath(rel, targetRel) + suffix
    warnings.push(`${rel}: rewrote url(${unquoted}) -> url(${rewritten})`)
    return `url(${rewritten})`
  })
}

function rewriteHtmlRootAttrs({ buildDir, fileAbs, rel, text, warnings, errors, rewriteRootRefs }) {
  return text.replace(/\b(src|href)=["']([^"']+)["']/g, (match, attr, rawUrl) => {
    if (isSkippableUrl(rawUrl)) return match
    const { pathname, suffix } = stripUrlDecorators(rawUrl)
    if (!pathname.startsWith('/')) return match
    const target = rootRelativeTarget(buildDir, pathname)
    if (!inBuild(buildDir, target) || !fs.existsSync(target)) {
      errors.push(`${rel}: unresolved ${attr}="${rawUrl}"`)
      return match
    }
    if (!rewriteRootRefs) {
      errors.push(`${rel}: absolute ${attr}="${rawUrl}" requires --rewrite-root-refs`)
      return match
    }
    const targetRel = toPosix(path.relative(buildDir, target))
    const rewritten = relPath(rel, targetRel) + suffix
    warnings.push(`${rel}: rewrote ${attr}="${rawUrl}" -> ${attr}="${rewritten}"`)
    return `${attr}="${rewritten}"`
  })
}

export function rewriteAssetRefs(buildDir, files, opts = {}) {
  const warnings = []
  const errors = []
  const rewriteRootRefs = opts.rewriteRootRefs !== false
  for (const fileAbs of files) {
    const ext = path.extname(fileAbs).toLowerCase()
    if (!TEXT_EXTS.has(ext)) continue
    const rel = toPosix(path.relative(buildDir, fileAbs))
    let text = fs.readFileSync(fileAbs, 'utf8')
    const before = text
    if (ext === '.css') {
      text = rewriteCssUrls({ buildDir, fileAbs, rel, text, warnings, errors, rewriteRootRefs })
    } else if (ext === '.html') {
      text = rewriteHtmlRootAttrs({ buildDir, fileAbs, rel, text, warnings, errors, rewriteRootRefs })
    }
    if (text !== before) fs.writeFileSync(fileAbs, text)
  }
  return { warnings, errors }
}

function wrapperSource(id, name) {
  const pascal = id
    .replace(/(^|[-_])([a-z0-9])/g, (_, __, ch) => ch.toUpperCase())
    .replace(/[^A-Za-z0-9]/g, '')
  const component = `Mobius${pascal || 'Static'}App`
  const safeName = JSON.stringify(name)
  return `import React from 'react'

export default function ${component}({ appId }) {
  const src = appId
    ? \`/app-assets/by-id/\${appId}/index.html\`
    : \`/app-assets/${id}/index.html\`

  return (
    <div style={{ height: '100%', width: '100%', background: '#10121f', overflow: 'hidden' }}>
      <iframe
        title={${safeName}}
        src={src}
        style={{ width: '100%', height: '100%', border: 0, display: 'block', background: '#10121f' }}
        allow="autoplay; fullscreen; gamepad"
      />
    </div>
  )
}
`
}

export function packageStaticApp(opts) {
  for (const key of ['id', 'name', 'version', 'description', 'buildDir', 'outDir']) {
    if (!opts[key]) throw new Error(`Missing required option --${key.replace(/[A-Z]/g, ch => `-${ch.toLowerCase()}`)}.`)
  }
  validateSlug(opts.id)
  validateRepoPath(opts.entry || 'index.jsx', 'entry')
  if (opts.icon) validateRepoPath(opts.icon, 'icon')

  const buildDir = path.resolve(opts.buildDir)
  const outDir = path.resolve(opts.outDir)
  if (!fs.existsSync(buildDir) || !fs.statSync(buildDir).isDirectory()) {
    throw new Error(`Build directory does not exist: ${buildDir}`)
  }
  if (!fs.existsSync(path.join(buildDir, 'index.html'))) {
    throw new Error(`Build directory must contain index.html: ${buildDir}`)
  }
  fs.mkdirSync(outDir, { recursive: true })
  const entry = opts.entry || 'index.jsx'
  const manifestPath = path.join(outDir, 'mobius.json')
  const entryPath = path.join(outDir, entry)
  for (const target of [manifestPath, entryPath]) {
    if (fs.existsSync(target) && !opts.force) {
      throw new Error(`${target} already exists. Pass --force to overwrite.`)
    }
  }

  const files = walkFiles(buildDir, Boolean(opts.includeSourcemaps))
  if (!files.length) throw new Error(`Build directory has no files: ${buildDir}`)

  const rewrite = rewriteAssetRefs(buildDir, files, {
    rewriteRootRefs: opts.rewriteRootRefs,
  })
  if (rewrite.errors.length) {
    throw new Error(`Static asset reference validation failed:\n${rewrite.errors.join('\n')}`)
  }

  const staticAssets = {}
  for (const abs of files) {
    const dest = toPosix(path.relative(buildDir, abs))
    const src = toPosix(path.relative(outDir, abs))
    validateRepoPath(dest, `static_assets.${dest}`)
    validateRepoPath(src, `static_assets.${dest}`)
    staticAssets[dest] = src
  }

  const manifest = {
    id: opts.id,
    name: opts.name,
    version: opts.version,
    description: opts.description,
    author: opts.author || undefined,
    license: opts.license || undefined,
    homepage: opts.homepage || undefined,
    entry,
    icon: opts.icon || undefined,
    offline_capable: Boolean(opts.offlineCapable),
    permissions: {
      cross_app_access: 'none',
      share_with_apps: 'none',
    },
    static_assets: staticAssets,
    runtime: {
      imports: ['react'],
      esm_deps: [],
    },
  }
  for (const [key, value] of Object.entries(manifest)) {
    if (value === undefined) delete manifest[key]
  }

  fs.mkdirSync(path.dirname(entryPath), { recursive: true })
  fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`)
  fs.writeFileSync(entryPath, wrapperSource(opts.id, opts.name))
  return { manifestPath, entryPath, assetCount: files.length, warnings: rewrite.warnings }
}

function main() {
  try {
    const opts = parseArgs(process.argv.slice(2))
    if (opts.help) {
      console.log(usage())
      return
    }
    const result = packageStaticApp(opts)
    for (const warning of result.warnings) {
      console.warn(`package-static-app: ${warning}`)
    }
    console.log(`package-static-app: wrote ${result.manifestPath}`)
    console.log(`package-static-app: wrote ${result.entryPath}`)
    console.log(`package-static-app: ${result.assetCount} static assets`)
  } catch (err) {
    console.error(`package-static-app: ${err.message}`)
    console.error(usage())
    process.exitCode = 1
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  main()
}
