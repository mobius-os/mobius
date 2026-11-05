// Opening a chat pins its network cost: the real App runs in Chromium over an
// in-page fake backend, and every /api request it makes is recorded.
import { test, before, after } from 'node:test'
import assert from 'node:assert/strict'
import { createServer as createHttpServer } from 'node:http'
import { createServer as createViteServer } from 'vite'
import { mkdtemp, rm, access, constants } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join, resolve } from 'node:path'

const root = resolve(import.meta.dirname, '../../../..')
const FIXTURE = '/src/components/Shell/__tests__/chatOpen.fixture.jsx'
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms))
let vite, http, browser, context, page, viteCache, origin

// CI installs Playwright's Chromium and must run this test; elsewhere CHROME_BIN
// or an installed Playwright Chromium is used, and without one the test is
// skipped (not failed) so `npm test` stays usable on a machine without one.
async function findChromium() {
  let chromium
  try { ({ chromium } = await import('@playwright/test')) } catch { return null }
  const executablePath = process.env.CHROME_BIN || chromium.executablePath()
  try { await access(executablePath, constants.X_OK) } catch {
    if (process.env.CHROME_BIN) throw Error(`CHROME_BIN is not an executable Chromium path: ${process.env.CHROME_BIN}`)
    return null
  }
  return { chromium, executablePath }
}
const found = await findChromium()
if (!found && process.env.CI) throw Error('Mounted Shell tests require Playwright Chromium in CI.')
const skip = found ? false : 'Chromium not found; set CHROME_BIN to run the mounted chat-open test.'

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
  browser = await found.chromium.launch({
    executablePath: found.executablePath,
    args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'],
  })
}

// Every test starts from a new browser context (an empty profile), so no test
// depends on the persisted workspace or query cache another one left behind.
// The viewport is the narrow phone where the slow open was measured. Playwright
// keeps the page focused, so window focus (a real reconciliation trigger) never
// fires at a load-dependent moment inside a measured window.
async function freshVisit(path) {
  await context?.close()
  context = await browser.newContext({
    viewport: { width: 426, height: 900 }, deviceScaleFactor: 1, isMobile: true,
  })
  page = await context.newPage()
  page.on('pageerror', error => process.stderr.write(`Browser exception: ${error.stack}\n`))
  await page.goto(`${origin}${path}`)
}
const evaluate = expression => page.evaluate(expression)
const until = (expression, timeout = 60000) => page.waitForFunction(expression, null, { timeout })

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
// The open is complete once its chat is painted and the system stream's
// reconnect barrier has read the chat list.
async function openSettled(chatId) {
  await until(`!!window.chatOpen?.systemOpened && ${painted(chatId)} && ${requested('/api/chats')}`)
  await settle()
}
// The drawer's rows leave the accessibility tree while it is closed, so a role
// locator finds only buttons the owner could tap; click() waits for one.
const button = label => page.getByRole('button', { name: label, exact: true })

before(setup)

after(async () => {
  await browser?.close()
  http?.closeAllConnections?.()
  if (http) await new Promise(resolve => http.close(resolve))
  if (vite) await vite.close()
  if (viteCache) await rm(viteCache, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
})

test('a deep link reads only the chat it opens, without a startup recovery read', { skip }, async () => {
  // A first visit leaves `previous` open in the persisted workspace.
  await freshVisit('/shell/?chat=previous')
  await openSettled('previous')
  // A later visit straight to another chat, with every persisted read stale.
  await page.goto(`${origin}/shell/?chat=target&skew=600000`)
  await openSettled('target')
  const requests = await take()

  assert.deepEqual(requests.filter(request => request.includes('previous')), [],
    'the restored chat the link replaced never mounts or reads')
  const detail = '/api/chats/target?limit=20&compact=1'
  const runtime = '/api/chats/target/runtime'
  // The startup readiness verdict is not recovery, so it re-reads nothing; the
  // one runtime read is the stream-open reconcile that follows the list read.
  assert.equal(count(requests, runtime), 1, 'no startup recovery runtime read')
  assert.ok(requests.indexOf('/api/chats') < requests.indexOf(runtime),
    'the runtime read is the stream-open reconcile')
  assert.equal(count(requests, '/api/chats/target/compact-progress'), 1)
  assert.equal(count(requests, '/api/apps/chat-artifacts/target'), 1)
  assert.equal(count(requests, '/api/ready'), 1, 'one startup readiness probe')
  assert.equal(count(requests, '/api/chats'), 1, 'one live list read: the stream-open barrier')
  // Activation reads the page; the stream-open reconcile then reads it once
  // more, after the list.
  assert.equal(count(requests, detail), 2)
  assert.ok(requests.indexOf('/api/chats') < requests.lastIndexOf(detail),
    'the reconcile read follows the barrier list read')
})

test('a drawer tap reads the chat it opens once', { skip }, async () => {
  await freshVisit('/shell/?chat=target')
  await openSettled('target')
  await button('Toggle navigation').click()
  await button('Chat other').waitFor()
  await settle({ quietMs: 500 })
  await take()
  await button('Chat other').click()
  await until(painted('other'))
  await until(requested('/api/chats/other/activity?limit=50'))
  await settle()
  const requests = await take()

  assert.deepEqual(requests.filter(request => request.startsWith('/api/chats/other')).sort(), [
    '/api/chats/other/activity?limit=50',
    '/api/chats/other/compact-progress',
    // Activation is the only reader of the first page.
    '/api/chats/other?limit=20&compact=1',
  ])
  assert.equal(count(requests, '/api/apps/chat-artifacts/other'), 1)
  assert.equal(count(requests, '/api/chats'), 0, 'opening a chat does not re-read the chat list')
  assert.deepEqual(requests.filter(request => request.includes('/chats/target')), [],
    'the chat left behind does not refresh')
})
