// Test-only browser/React boundaries. All mount, error, and retry decisions
// come from app-frame.html; declaration ranges are parsed, never brace-sliced.
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

// acorn is a frontend dependency: resolve it from the frontend tree (CI installs
// it there) or the explicitly supplied one, as check-built-globals.mjs does.
const configuredModules = process.env.MOBIUS_FRONTEND_NODE_MODULES
const dependencyRoot = configuredModules
  ? path.resolve(
      path.basename(configuredModules) === 'node_modules'
        ? path.dirname(configuredModules)
        : configuredModules,
    )
  : path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../frontend')
const { parse } = createRequire(path.join(dependencyRoot, 'package.json'))('acorn')
const options = JSON.parse(readFileSync(0, 'utf8'))
const scripts = [...options.html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)]
  .map(([, attributes, source]) => ({
    source,
    ast: parse(source, {
      ecmaVersion: 'latest',
      sourceType: /type\s*=\s*["']module["']/.test(attributes) ? 'module' : 'script',
    }),
  }))
const owners = ['handleFrameError', 'loadModule'].map(name => {
  const matches = scripts.filter(({ ast }) => ast.body.some(
    node => node.type === 'FunctionDeclaration' && node.id.name === name,
  ))
  if (matches.length !== 1) throw new Error(`Expected one script owning ${name}`)
  return matches[0]
})
const declarations = owners.flatMap(({ source, ast }) => ast.body.filter(node => {
  if (['FunctionDeclaration', 'VariableDeclaration'].includes(node.type)) return true
  // Preserve the real extension classifier used by handleFrameError, without
  // executing unrelated top-level DOM listeners or the frame-init protocol.
  const expression = node.type === 'ExpressionStatement' && node.expression
  return expression?.type === 'AssignmentExpression'
    && expression.left.type === 'MemberExpression'
    && expression.left.object.name === 'window'
    && expression.left.property.name === '__isExtensionError'
}).map(node => source.slice(node.start, node.end))).join('\n')

const messages = []
const logs = []
const elements = new Map()
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, {
      style: {}, textContent: '',
      classList: new Set(),
    })
    return elements.get(id)
  },
}
// classList.add and Set.add share the DOM operation exercised by showErr.
const window = {
  location: { origin: 'https://frame.test', href: 'https://frame.test/frame' },
  parent: { postMessage: (message, origin) => messages.push({ message, origin }) },
}
const timers = []
const delays = []
const setTimeout = (callback, milliseconds) => {
  delays.push(milliseconds)
  timers.push(callback)
  return timers.length
}
const realm = {}
const factory = new Function('window', 'document', 'globalThis', 'location',
  'console', 'setTimeout', 'atob', `${declarations}
  return {
    loadModule, handleFrameError,
    abi: COMPILED_RUNTIME_ABI,
    setup(runtime, transport) {
      currentToken = 'test-token';
      initialized = true;
      globalThis.__mobiusCompiledRuntime = runtime;
      importBrokeredModule = transport;
    },
    isInitialized: () => initialized,
  };`)
const frame = factory(window, document, realm, window.location, {
  error: (...args) => logs.push(args.join(' ')),
  warn: (...args) => logs.push(args.join(' ')),
}, setTimeout, value => Buffer.from(value, 'base64').toString('binary'))

const Fragment = Symbol('Fragment')
let pendingRender = null
let rootOptions = null
let committedRefs = []
let renderCount = 0
let appRenderCount = 0
let renderThrows = false
const runtime = {
  abi: frame.abi,
  Fragment,
  createElement: (type, props, ...children) => ({ type, props: props || {}, children }),
  createRoot(_container, callbacks) {
    rootOptions = callbacks
    return { render(tree) { renderCount += 1; pendingRender = tree } }
  },
}
function App() {
  appRenderCount += 1
  if (renderThrows) throw new Error('initial app render failed')
  return runtime.createElement('main', null, 'visible app')
}
function commit() {
  if (!pendingRender) return
  const tree = pendingRender
  pendingRender = null
  const refs = []
  function render(node) {
    if (node == null || typeof node !== 'object') return
    if (typeof node.type === 'function') return render(node.type(node.props))
    for (const child of node.children) render(child)
    if (typeof node.props.ref === 'function') refs.push(node.props.ref)
  }
  // React does not attach any host refs if rendering a sibling throws. Ref
  // attachment is a separate commit step, never part of createElement/render.
  try {
    render(tree)
  } catch (error) {
    rootOptions.onUncaughtError(error, { componentStack: 'in App' })
    return
  }
  committedRefs = refs
  for (const ref of refs) ref({ attached: true })
}

const imports = []
let failures = []
frame.setup(runtime, async retry => {
  imports.push(retry)
  const failure = failures.shift()
  if (failure) {
    const error = new Error(failure.message || 'module import failed')
    Object.assign(error, failure)
    throw error
  }
  return { default: App }
})
function snapshot() {
  return {
    mounted: Boolean(window.__frameMounted),
    messages: messages.map(({ message }) => message.type),
    origins: messages.map(({ origin }) => origin),
    panel: document.getElementById('error-panel').classList.has('visible'),
    rootHidden: document.getElementById('root').style.display === 'none',
    title: document.getElementById('error-title').textContent,
    detail: document.getElementById('error-detail').textContent,
    initialized: frame.isInitialized(),
    imports: [...imports], delays: [...delays], renderCount, appRenderCount,
    configPresent: Object.hasOwn(realm, '__mobiusRuntimeConfig'),
  }
}
if (options.postmount) {
  await frame.loadModule()
  commit()
  if (!window.__frameMounted) throw new Error('Post-mount setup did not commit')
  imports.length = 0
}
failures = [...(options.failures || [])]
renderThrows = options.renderThrows === true
const initial = snapshot()
const loading = frame.loadModule()
// Let resolved/rejected import promises reach the real retry delay or render.
await new Promise(setImmediate)
const beforeDelay = snapshot()
if (timers.length) timers.shift()()
await loading
const beforeCommit = snapshot()
if (options.commit !== false) commit()
const afterCommit = snapshot()
// Exercise the refs collected from the real rendered tree, not a directly
// invoked MountSignal. React may detach/re-attach without a new first mount.
for (const ref of committedRefs) { ref(null); ref({ attached: true }) }
const afterReattach = snapshot()
if (options.globalError) frame.handleFrameError('Unhandled error', options.globalError)
process.stdout.write(JSON.stringify({
  initial, beforeDelay, beforeCommit, afterCommit, afterReattach,
  final: snapshot(), logs,
}))
