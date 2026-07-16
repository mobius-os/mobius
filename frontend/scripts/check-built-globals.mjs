// Reject built shell modules that reference undeclared runtime identifiers.

import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

// Source-checkout tests mount the current script at /workspace while using the
// image-baked dependency tree. Resolve Babel from an explicit dependency root
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
const { parse } = require('@babel/parser')
const traverseModule = require('@babel/traverse')

const traverse = traverseModule.default

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

  // Window + DOM globals used by the shell and bundled UI dependencies.
  'AbortController', 'AbortSignal', 'atob', 'Blob', 'BroadcastChannel', 'btoa',
  'caches', 'cancelAnimationFrame', 'clearInterval', 'clearTimeout', 'console',
  'createImageBitmap', 'crypto', 'CSS', 'CustomEvent', 'document', 'DOMParser',
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

  function walk(dir) {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const file = path.join(dir, entry.name)
      if (entry.isDirectory()) walk(file)
      else if (entry.isFile() && entry.name.endsWith('.js')) files.push(file)
    }
  }

  if (fs.existsSync(assetsDir)) walk(assetsDir)
  const sw = path.join(buildDir, 'sw.js')
  if (fs.existsSync(sw)) files.push(sw)
  return files.sort()
}

function findUnbound(buildDir) {
  const failures = new Map()

  for (const file of jsFiles(buildDir)) {
    let ast
    try {
      ast = parse(fs.readFileSync(file, 'utf8'), {
        sourceType: 'unambiguous',
        plugins: ['importMeta', 'topLevelAwait'],
      })
    } catch (error) {
      const rel = path.relative(buildDir, file)
      throw new Error(`could not parse ${rel}: ${error.message}`)
    }

    traverse(ast, {
      ReferencedIdentifier(identifierPath) {
        const { name, loc } = identifierPath.node
        if (ALLOWED_GLOBALS.has(name)) return
        if (identifierPath.scope.hasBinding(name, true)) return

        const spots = failures.get(name) || []
        if (spots.length < 6) {
          const rel = path.relative(buildDir, file)
          spots.push(`${rel}:${loc?.start.line ?? '?'}:${loc?.start.column ?? '?'}`)
        }
        failures.set(name, spots)
      },
    })
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
