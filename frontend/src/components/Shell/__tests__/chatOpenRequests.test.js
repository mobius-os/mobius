// Opening a chat pins its network cost: the real App runs in Chromium over an
// in-page fake backend, and every /api request it makes is recorded.
import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { createServer as createHttpServer } from 'node:http'
import { createServer as createViteServer } from 'vite'
import { mkdtemp, rm, readFile, access, constants } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

const root = resolve(import.meta.dirname, '../../../..')
const FIXTURE = '/src/components/Shell/__tests__/chatOpen.fixture.jsx'
const CDP_TIMEOUT_MS = 30000
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
let vite, http, chrome, profile, viteCache, socket, origin
let nextId = 0
const awaiting = new Map()

// CI exports CHROME_BIN and must run this test; elsewhere an installed
// Playwright Chromium is used, and without one the test is skipped (not
// failed) so `npm test` stays usable on a machine without a browser.
async function findChromium() {
  if (process.env.CHROME_BIN) {
    try { await access(process.env.CHROME_BIN, constants.X_OK) }
    catch { throw Error(`CHROME_BIN is not an executable Chromium path: ${process.env.CHROME_BIN}`) }
    return process.env.CHROME_BIN
  }
  try {
    const { chromium } = await import('@playwright/test')
    const candidate = chromium.executablePath()
    await access(candidate, constants.X_OK)
    return candidate
  } catch {
    return null
  }
}
const binary = await findChromium()
if (!binary && process.env.CI) throw Error('Mounted Shell tests require Chromium in CI: set CHROME_BIN.')
const skip = binary ? false : 'Chromium not found; set CHROME_BIN to run the mounted chat-open test.'

async function setup() {
  if (skip) return
  viteCache = await mkdtemp(join(tmpdir(), 'chat-open-vite-'))
  vite = await createViteServer({ root, cacheDir: viteCache, configFile: resolve(root, 'vite.config.js'),
    server: { middlewareMode: true, hmr: false }, appType: 'custom', logLevel: 'error' })
  http = createHttpServer(async (req, res) => {
    if (req.url.startsWith('/shell/')) {
      res.writeHead(200, { 'Content-Type': 'text/html' })
      res.end(await vite.transformIndexHtml(req.url, `<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"></head><body><div id="root"></div><script type="module" src="${FIXTURE}"></script></body></html>`))
    } else vite.middlewares(req, res)
  })
  await new Promise(resolve => http.listen(0, '127.0.0.1', resolve))
  origin = `http://127.0.0.1:${http.address().port}`
  profile = await mkdtemp(join(tmpdir(), 'chat-open-chrome-'))
  chrome = spawn(binary, ['--headless=new', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage',
    '--remote-debugging-port=0', `--user-data-dir=${profile}`, 'about:blank'], { stdio: 'ignore' })
  let debugPort
  for (let i = 0; i < 100 && !debugPort; i++) {
    try { debugPort = Number((await readFile(join(profile, 'DevToolsActivePort'), 'utf8')).split('\n')[0]) }
    catch { await sleep(100) }
  }
  assert.ok(debugPort, 'Chromium CDP port available')
  let page
  for (let i = 0; i < 50 && !page; i++) {
    const pages = await fetch(`http://127.0.0.1:${debugPort}/json/list`).then(r => r.json())
    page = pages.find(p => p.type === 'page')
    if (!page) await sleep(100)
  }
  assert.ok(page, 'blank page opened')
  socket = new WebSocket(page.webSocketDebuggerUrl)
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true })
    socket.addEventListener('error', reject, { once: true })
  })
  socket.onmessage = event => {
    const packet = JSON.parse(event.data)
    if (packet.method === 'Runtime.exceptionThrown') {
      process.stderr.write(`Browser exception: ${JSON.stringify(packet.params.exceptionDetails).slice(0, 2000)}\n`)
    }
    const waiter = packet.id && awaiting.get(packet.id)
    if (!waiter) return
    awaiting.delete(packet.id)
    clearTimeout(waiter.timer)
    packet.error ? waiter.reject(Error(packet.error.message)) : waiter.resolve(packet.result)
  }
  await cdp('Runtime.enable')
  await cdp('Page.enable')
  // The narrow phone viewport where the slow open was measured.
  await cdp('Emulation.setDeviceMetricsOverride', { width: 426, height: 900, deviceScaleFactor: 1, mobile: true })
  // Window focus is a real reconciliation trigger: ChatView re-reads runtime
  // and the connectivity monitor re-probes readiness (a recovery edge). A
  // headless page otherwise gains focus at the first synthetic input, at a
  // load-dependent moment that can fall inside a measured window. Keep the
  // page focused throughout so only the open under test issues requests.
  await cdp('Emulation.setFocusEmulationEnabled', { enabled: true })
}

function cdp(method, params = {}) {
  const id = ++nextId
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => { awaiting.delete(id); reject(Error(`CDP timed out: ${method}`)) }, CDP_TIMEOUT_MS)
    awaiting.set(id, { resolve, reject, timer })
    socket.send(JSON.stringify({ id, method, params }))
  })
}
async function evaluate(expression) {
  const result = await cdp('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true })
  if (result.exceptionDetails) throw Error(result.exceptionDetails.text)
  return result.result.value
}
async function until(expression, timeout = 60000) {
  const deadline = Date.now() + timeout
  while (Date.now() < deadline) {
    try { if (await evaluate(expression)) return } catch { /* page still loading */ }
    await sleep(50)
  }
  throw Error(`Timed out: ${expression}`)
}

// Web-push registration retries on its own schedule and is not chat-open work.
const chatOpenWork = request => !request.includes('/api/push/')
// Waits for the open's own completion signals first, then for a quiet period,
// so a slow machine pausing mid-open is not mistaken for the end.
async function settle({ quietMs = 1500, timeout = 60000 } = {}) {
  const deadline = Date.now() + timeout
  let seen = -1
  let quietSince = Date.now()
  while (Date.now() < deadline) {
    const count = (await evaluate('window.chatOpen.peek()')).filter(chatOpenWork).length
    if (count !== seen) { seen = count; quietSince = Date.now() }
    else if (Date.now() - quietSince >= quietMs) return
    await sleep(100)
  }
  throw Error('Requests never settled')
}
async function take() {
  return (await evaluate('window.chatOpen.take()')).filter(chatOpenWork)
}
const painted = chatId => `!!document.querySelector('[data-chat-surface="painted"][data-chat-id="${chatId}"]')`
const requested = entry => `window.chatOpen.peek().includes(${JSON.stringify(entry)})`
const count = (requests, entry) => requests.filter(request => request === entry).length
// Every test starts from an empty browser profile for this origin, so no test
// depends on the persisted workspace or query cache another one left behind.
async function freshVisit(path) {
  await cdp('Page.navigate', { url: 'about:blank' })
  await cdp('Storage.clearDataForOrigin', { origin, storageTypes: 'all' })
  await cdp('Page.navigate', { url: `${origin}${path}` })
}
// The open is complete once its chat is painted and the system stream's
// reconnect barrier has read the chat list.
async function openSettled(chatId) {
  await until(`!!window.chatOpen?.systemOpened && ${painted(chatId)} && ${requested('/api/chats')}`)
  await settle()
}
// A button by its accessible label or text that is on screen (drawer rows
// stay mounted off-canvas while the drawer is closed).
const visibleButton = label => `[...document.querySelectorAll('button')]
  .find(b => (b.getAttribute('aria-label') || b.innerText).trim() === ${JSON.stringify(label)}
    && b.getBoundingClientRect().x >= 0)`
async function tap(label) {
  const point = await evaluate(`(() => {
    const button = ${visibleButton(label)}
    if (!button) return null
    const r = button.getBoundingClientRect()
    return { x: r.x + r.width / 2, y: r.y + r.height / 2 }
  })()`)
  assert.ok(point, `button ${label}`)
  for (const type of ['mouseMoved', 'mousePressed', 'mouseReleased']) {
    await cdp('Input.dispatchMouseEvent', {
      type, x: point.x, y: point.y, button: 'left', clickCount: type === 'mouseMoved' ? 0 : 1,
    })
  }
}

before(setup)

after(async () => {
  socket?.close()
  if (chrome && chrome.exitCode === null && chrome.signalCode === null) {
    const exited = new Promise(resolve => chrome.once('exit', resolve))
    chrome.kill('SIGTERM')
    const timer = setTimeout(() => chrome.kill('SIGKILL'), 3000)
    await exited
    clearTimeout(timer)
  }
  http?.closeAllConnections?.()
  if (http) await new Promise(resolve => http.close(resolve))
  if (vite) await vite.close()
  // Chromium's helper processes can still be flushing the profile.
  if (profile) await rm(profile, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
  if (viteCache) await rm(viteCache, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
})

test('a deep link reads only the chat it opens, once, without a chained runtime read', { skip }, async () => {
  // A first visit leaves `previous` open in the persisted workspace.
  await freshVisit('/shell/?chat=previous')
  await openSettled('previous')
  // A later visit straight to another chat, with every persisted read stale.
  await cdp('Page.navigate', { url: `${origin}/shell/?chat=target&skew=600000` })
  await openSettled('target')
  const requests = await take()

  assert.deepEqual(requests.filter(request => request.includes('previous')), [],
    'the restored chat the link replaced never mounts or reads')
  const detail = '/api/chats/target?limit=20&compact=1'
  assert.equal(count(requests, '/api/chats/target/runtime'), 0,
    'neither the startup readiness verdict nor the stream-open reconcile chains a runtime read')
  assert.equal(count(requests, '/api/chats/target/compact-progress'), 1)
  assert.equal(count(requests, '/api/apps/chat-artifacts/target'), 1)
  assert.equal(count(requests, '/api/ready'), 1, 'one startup readiness probe')
  assert.equal(count(requests, '/api/chats'), 1, 'one live list read: the stream-open barrier')
  // Activation reads the page; the stream-open barrier then reconciles the
  // visible chat with exactly one more detail read, issued after the list.
  assert.equal(count(requests, detail), 2)
  assert.ok(requests.indexOf('/api/chats') < requests.lastIndexOf(detail),
    'the reconcile read follows the barrier list read')
})

test('a drawer tap reads the chat it opens once', { skip }, async () => {
  await freshVisit('/shell/?chat=target')
  await openSettled('target')
  await tap('Toggle navigation')
  await until(`!!${visibleButton('Chat other')}`)
  await settle({ quietMs: 500 })
  await take()
  await tap('Chat other')
  await until(painted('other'))
  await until(requested('/api/chats/other/activity?limit=50'))
  await settle()
  const requests = await take()

  assert.deepEqual(requests.filter(request => request.startsWith('/api/chats/other')).sort(), [
    '/api/chats/other/activity?limit=50',
    '/api/chats/other/compact-progress',
    // The composer's Changes overview.
    '/api/chats/other/edit-diffs',
    // Intent warming on pointerdown and the activation share this one read.
    '/api/chats/other?limit=20&compact=1',
  ])
  assert.equal(count(requests, '/api/apps/chat-artifacts/other'), 1)
  assert.equal(count(requests, '/api/chats'), 0, 'opening a chat does not re-read the chat list')
  assert.deepEqual(requests.filter(request => request.includes('/chats/target')), [],
    'the chat left behind does not refresh')
})
