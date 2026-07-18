// Reject built shell modules that reference undeclared runtime identifiers.

import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

// Source-checkout tests mount the current script at /workspace while using the
// image-baked dependency tree. Resolve analyzer dependencies from an explicit root
// when supplied; normal development and production resolve beside this script.
const scriptFrontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const configuredModules = process.env.MOBIUS_FRONTEND_NODE_MODULES
const dependencyRoot = configuredModules
  ? path.resolve(
      path.basename(configuredModules) === 'node_modules'
        ? path.dirname(configuredModules)
        : configuredModules,
    )
  : scriptFrontend
const require = createRequire(path.join(dependencyRoot, 'package.json'))
const { parse } = require('acorn')
const { analyze } = require('eslint-scope')

// Vite bundles application and dependency code together. These are the
// runtime globals intentionally supplied by JavaScript, the browser, a service
// worker, or the optional development hooks used by bundled dependencies.
// Keep this explicit: a newly used browser API gets one reviewable allowlist
// entry, while a refactor typo such as `ioBounceTimer` remains a hard failure.
const ALLOWED_GLOBALS = new Set([
  // ECMAScript globals.
  'AggregateError', 'Array', 'ArrayBuffer', 'Atomics', 'BigInt',
  'BigInt64Array', 'BigUint64Array', 'Boolean', 'DataView', 'Date',
  'decodeURI', 'decodeURIComponent', 'encodeURI', 'encodeURIComponent',
  'Error', 'escape', 'eval', 'EvalError', 'FinalizationRegistry', 'Float32Array',
  'Float64Array', 'Function', 'globalThis', 'Infinity', 'Int8Array',
  'Int16Array', 'Int32Array', 'Intl', 'isFinite', 'isNaN', 'JSON', 'Map',
  'Math', 'NaN', 'Number', 'Object', 'parseFloat', 'parseInt', 'Promise',
  'Proxy', 'RangeError', 'ReferenceError', 'Reflect', 'RegExp', 'Set',
  'SharedArrayBuffer', 'String', 'Symbol', 'SyntaxError', 'TypeError',
  'Uint8Array', 'Uint8ClampedArray', 'Uint16Array', 'Uint32Array',
  'undefined', 'unescape', 'URIError', 'WeakMap', 'WeakRef', 'WeakSet',
  'WebAssembly',

  // Window, DOM, and worker globals used by the shell and its dependencies.
  'AbortController', 'AbortSignal', 'atob', 'Blob', 'BroadcastChannel', 'btoa',
  'caches', 'cancelAnimationFrame', 'clearInterval', 'clearTimeout', 'console',
  'createImageBitmap', 'crypto', 'CSS', 'CustomEvent', 'document',
  'DOMMatrixReadOnly', 'DOMParser',
  'Element', 'Event', 'EventSource', 'fetch', 'File', 'FileReader', 'FormData',
  'getComputedStyle', 'Headers', 'history', 'HTMLElement', 'HTMLInputElement',
  'indexedDB', 'IntersectionObserver', 'localStorage', 'location', 'matchMedia',
  'MessageChannel', 'MessageEvent', 'MutationObserver', 'navigation',
  'navigator', 'Node', 'NodeFilter', 'Notification', 'performance',
  'queueMicrotask', 'ReadableStream', 'reportError', 'Request',
  'requestAnimationFrame', 'requestIdleCallback', 'ResizeObserver', 'Response',
  'screen', 'sessionStorage', 'setImmediate', 'setInterval', 'setTimeout',
  'ShadowRoot', 'TextDecoder', 'TextEncoder', 'URL', 'URLSearchParams',
  'WebSocket', 'window', 'Worker', 'XMLHttpRequest',

  // Service-worker globals and names emitted by Workbox's generated wrapper.
  'clients', 'ExtendableEvent', 'FetchEvent', 'registration', 'self', '_',

  // Optional globals probed by bundled dependencies. Their reads are guarded
  // (usually with typeof); the browser does not have to define them.
  '__REACT_DEVTOOLS_GLOBAL_HOOK__', '__webpack_nonce__', 'arguments', 'Buffer',
  'global', 'process',
])

function jsFiles(buildDir) {
  const files = []
  const assetsDir = path.join(buildDir, 'assets')
  const isJavaScript = name => name.endsWith('.js') || name.endsWith('.mjs')

  function walk(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const file = path.join(dir, entry.name)
      if (entry.isDirectory()) walk(file)
      else if (entry.isFile() && isJavaScript(entry.name)) files.push(file)
    }
  }

  if (fs.existsSync(assetsDir)) walk(assetsDir)
  // Vendor assets are managed separately. Inspect Vite assets and root
  // modules such as sw.js and mobius-runtime.js.
  for (const entry of fs.readdirSync(buildDir, { withFileTypes: true })) {
    if (entry.isFile() && isJavaScript(entry.name)) {
      files.push(path.join(buildDir, entry.name))
    }
  }
  return files.sort()
}

function findUnbound(buildDir) {
  const failures = new Map()
  for (const file of jsFiles(buildDir)) {
    let ast
    try {
      ast = parse(fs.readFileSync(file, 'utf8'), {
        ecmaVersion: 'latest',
        sourceType: 'module',
        locations: true,
        ranges: true,
      })
    } catch (error) {
      throw new Error(`could not parse ${path.relative(buildDir, file)}: ${error.message}`)
    }

    const scopeManager = analyze(ast, {
      ecmaVersion: 2024,
      sourceType: 'module',
      optimistic: true,
      ignoreEval: true,
    })
    for (const reference of scopeManager.globalScope.through) {
      const { name, loc } = reference.identifier
      if (ALLOWED_GLOBALS.has(name)) continue
      const spots = failures.get(name) || []
      if (spots.length < 6) {
        spots.push(
          `${path.relative(buildDir, file)}:${loc?.start.line ?? '?'}:${loc?.start.column ?? '?'}`,
        )
      }
      failures.set(name, spots)
    }
  }
  return failures
}

const buildDir = path.resolve(process.argv[2] || '')
if (!process.argv[2] || !fs.existsSync(buildDir)) {
  console.error('Usage: node check-built-globals.mjs <built-frontend-dir>')
  process.exit(2)
}

try {
  const failures = findUnbound(buildDir)
  if (failures.size) {
    console.error('Built shell contains undeclared runtime identifiers:')
    for (const [name, spots] of [...failures].sort(([a], [b]) => a.localeCompare(b))) {
      console.error(`  ${name}: ${spots.join(', ')}`)
    }
    console.error(
      'Declare/import each application identifier, or add an intentional '
      + 'browser/worker global to ALLOWED_GLOBALS.',
    )
    process.exit(1)
  }
} catch (error) {
  console.error(`Built-global validation failed: ${error.message}`)
  process.exit(2)
}
