import { chromium } from '/home/hmzmrzx/projects/mobius/node_modules/playwright/index.mjs'
import fs from 'node:fs/promises'
import path from 'node:path'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8043'
const USER = process.env.MOBIUS_USER || 'admin'
const PASS = process.env.MOBIUS_PASS || 'admin'
const PROMPT = process.env.REATTACH_PROMPT
  || 'Write a thorough step-by-step 500-word explanation of how rainbows form, take your time'
const runId = new Date().toISOString().replace(/[:.]/g, '-')
const outDir = path.resolve('tests/artifacts', `reattach-${runId}`)
const events = []

function log(event) {
  const row = { t: new Date().toISOString(), ...event }
  events.push(row)
  console.log(JSON.stringify(row))
}

async function api(pathname, { token, method = 'GET', body } = {}) {
  const headers = {}
  if (token) headers.Authorization = `Bearer ${token}`
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  const res = await fetch(`${BASE}${pathname}`, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  const text = await res.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch { data = text }
  return { res, data }
}

async function token() {
  const res = await fetch(`${BASE}/api/auth/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username: USER, password: PASS }),
  })
  if (!res.ok) throw new Error(`token failed ${res.status}`)
  return (await res.json()).access_token
}

async function screenshot(page, name) {
  const file = path.join(outDir, `${String(events.length).padStart(3, '0')}-${name}.png`)
  await page.screenshot({ path: file, fullPage: true })
  log({ kind: 'screenshot', name, file })
  return file
}

async function visibleState(page) {
  return page.evaluate(() => {
    const assistants = [...document.querySelectorAll('.chat__msg--assistant')]
      .map((el, index) => ({
        index,
        text: (el.textContent || '').replace(/\s+/g, ' ').trim(),
        thinking: !!el.querySelector('.chat__thinking'),
        streaming: el.tagName === 'LI' && !el.dataset.ts,
      }))
    const streamItems = [...document.querySelectorAll('.chat__msg--assistant')]
      .filter(el => !el.querySelector('.chat__thinking'))
    return {
      activeChat: localStorage.getItem('moebius_active_chat'),
      stopVisible: !!document.querySelector('button[aria-label="Stop"]'),
      sendVisible: !!document.querySelector('button[aria-label="Send"]'),
      drawerOpen: !!document.querySelector('.drawer--open'),
      assistantCount: assistants.length,
      assistantTexts: assistants.map(a => a.text),
      lastAssistantText: assistants.at(-1)?.text || '',
      hasNonThinkingAssistant: streamItems.some(el => (el.textContent || '').trim().length > 0),
      htmlFlags: {
        chatScroll: !!document.querySelector('.chat__scroll'),
        form: !!document.querySelector('.chat__form'),
        empty: !!document.querySelector('.chat__empty-wrap'),
      },
    }
  })
}

async function waitForShell(page) {
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
      || document.querySelector('.chat__scroll')
      || document.querySelector('.chat__form')),
    { timeout: 30000 },
  )
}

async function sendPrompt(page) {
  const input = page.getByRole('textbox', { name: 'Message Möbius…' })
  await input.fill(PROMPT)
  await page.keyboard.press('Enter')
  await page.waitForSelector('button[aria-label="Stop"]', { timeout: 10000 })
}

async function waitForAssistantText(page, { minLength = 8, timeout = 45000 } = {}) {
  const start = Date.now()
  while (Date.now() - start < timeout) {
    const state = await visibleState(page)
    const text = state.lastAssistantText
    if (text.length >= minLength && !/^(\.|\s)*$/.test(text)) return state
    await page.waitForTimeout(300)
  }
  throw new Error(`assistant text did not reach ${minLength} chars`)
}

async function openDrawer(page) {
  await page.evaluate(() => {
    const btn = document.querySelector('[aria-label="Toggle navigation"], [aria-expanded]')
    if (btn && btn.getAttribute('aria-expanded') !== 'true') btn.click()
  })
  await page.waitForFunction(() => !!document.querySelector('.drawer--open'), { timeout: 5000 })
}

async function newChatFromDrawer(page) {
  await openDrawer(page)
  await page.evaluate(() => document.querySelector('.drawer__item--new')?.click())
  await page.waitForFunction(() => !document.querySelector('.drawer--open'), { timeout: 5000 })
}

async function returnToStreamingChatFromDrawer(page, chatId) {
  await openDrawer(page)
  const clicked = await page.evaluate((id) => {
    const rows = [...document.querySelectorAll('.drawer__row')]
    const streaming = rows.find(row => row.querySelector('.drawer__streaming-dot'))
    if (streaming) {
      streaming.querySelector('.drawer__item')?.click()
      return 'streaming-dot'
    }
    for (const row of rows) {
      const text = row.querySelector('.drawer__item-text')?.textContent || ''
      if (text.includes('rainbows') || text.includes('reattach')) {
        row.querySelector('.drawer__item')?.click()
        return text
      }
    }
    const fallback = rows.find(row => !row.textContent.includes('Settings'))
    fallback?.querySelector('.drawer__item')?.click()
    return fallback ? 'fallback-first-chat-row' : null
  }, chatId)
  log({ kind: 'drawer-return-click', clicked })
  await page.waitForFunction(
    id => localStorage.getItem('moebius_active_chat') === id,
    chatId,
    { timeout: 10000 },
  )
  await page.waitForFunction(() => !document.querySelector('.drawer--open'), { timeout: 5000 })
}

async function cycle(page, tokenValue, chatId, name, delayMs) {
  await page.waitForTimeout(delayMs)
  const before = await visibleState(page)
  log({ kind: 'cycle-before', name, delayMs, before })
  await screenshot(page, `${name}-before-away`)

  await newChatFromDrawer(page)
  await page.waitForFunction(id => localStorage.getItem('moebius_active_chat') !== id, chatId, { timeout: 10000 })
  log({ kind: 'cycle-away', name, away: await visibleState(page) })
  await screenshot(page, `${name}-away-new-chat`)

  await returnToStreamingChatFromDrawer(page, chatId)
  await page.waitForTimeout(700)
  const after = await visibleState(page)
  const detail = await api(`/api/chats/${chatId}?limit=20`, { token: tokenValue })
  log({
    kind: 'cycle-after',
    name,
    after,
    api: {
      status: detail.res.status,
      running: detail.data?.running,
      messageCount: detail.data?.messages?.length,
      lastRole: detail.data?.messages?.at(-1)?.role,
      lastContentLength: detail.data?.messages?.at(-1)?.content?.length || 0,
    },
  })
  await screenshot(page, `${name}-after-return`)

  const missing = detail.data?.running
    && (detail.data?.messages?.at(-1)?.role === 'assistant')
    && (detail.data?.messages?.at(-1)?.content?.length || 0) > 0
    && !after.hasNonThinkingAssistant
  log({ kind: 'assertion', name, missing })
  return { missing, after }
}

async function reloadCycle(page, tokenValue, chatId, name, delayMs) {
  await page.waitForTimeout(delayMs)
  log({ kind: 'reload-before', name, before: await visibleState(page) })
  await screenshot(page, `${name}-before-reload`)
  await page.reload({ waitUntil: 'domcontentloaded' })
  await waitForShell(page)
  await page.waitForTimeout(1000)
  const after = await visibleState(page)
  const detail = await api(`/api/chats/${chatId}?limit=20`, { token: tokenValue })
  log({
    kind: 'reload-after',
    name,
    after,
    api: {
      status: detail.res.status,
      running: detail.data?.running,
      messageCount: detail.data?.messages?.length,
      lastRole: detail.data?.messages?.at(-1)?.role,
      lastContentLength: detail.data?.messages?.at(-1)?.content?.length || 0,
    },
  })
  await screenshot(page, `${name}-after-reload`)
  const missing = detail.data?.running
    && (detail.data?.messages?.at(-1)?.role === 'assistant')
    && (detail.data?.messages?.at(-1)?.content?.length || 0) > 0
    && !after.hasNonThinkingAssistant
  log({ kind: 'assertion', name, missing })
  return { missing, after }
}

await fs.mkdir(outDir, { recursive: true })
const tokenValue = await token()
await api('/api/owner/walkthrough/complete', { token: tokenValue, method: 'POST' })
const list = await api('/api/chats', { token: tokenValue })
if (Array.isArray(list.data)) {
  for (const chat of list.data) {
    await api(`/api/chats/${chat.id}`, { token: tokenValue, method: 'DELETE' })
  }
}
const created = await api('/api/chats', {
  token: tokenValue,
  method: 'POST',
  body: { title: `reattach-${runId}` },
})
if (!created.res.ok) throw new Error(`create chat failed ${created.res.status}`)
const chatId = created.data.id
log({ kind: 'created-chat', chatId, outDir })

const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] })
const context = await browser.newContext({ viewport: { width: 1280, height: 900 } })
await context.addInitScript(({ tokenValue: initToken, chatId: initChat }) => {
  localStorage.setItem('token', initToken)
  localStorage.setItem('moebius_active_chat', initChat)
  localStorage.setItem('mobius:walkthrough-completed', '1')
}, { tokenValue, chatId })
const page = await context.newPage()

page.on('request', req => {
  const url = req.url()
  if (url.includes('/api/chats/') && (url.includes('/stream') || url.includes('?limit=20'))) {
    log({ kind: 'request', method: req.method(), url })
  }
})
page.on('response', res => {
  const url = res.url()
  if (url.includes('/api/chats/') && (url.includes('/stream') || url.includes('?limit=20'))) {
    log({ kind: 'response', status: res.status(), url })
  }
})
page.on('console', msg => {
  if (msg.type() === 'error') log({ kind: 'console-error', text: msg.text() })
})
page.on('pageerror', err => log({ kind: 'pageerror', text: err.message }))

try {
  await page.goto(`${BASE}/shell/?chat=${chatId}`, { waitUntil: 'domcontentloaded' })
  await waitForShell(page)
  await screenshot(page, 'initial')
  await sendPrompt(page)
  await screenshot(page, 'sent')
  const firstText = await waitForAssistantText(page, { minLength: 12, timeout: 60000 })
  log({ kind: 'first-assistant-text', state: firstText })
  await screenshot(page, 'first-assistant-text')

  const results = []
  results.push(await cycle(page, tokenValue, chatId, 'early-drawer-return', 1000))
  results.push(await cycle(page, tokenValue, chatId, 'mid-drawer-return', 5000))
  results.push(await reloadCycle(page, tokenValue, chatId, 'mid-full-reload', 3000))
  results.push(await cycle(page, tokenValue, chatId, 'late-drawer-return', 9000))

  const reproduced = results.some(r => r.missing)
  log({ kind: 'summary', reproduced, outDir })
  await fs.writeFile(path.join(outDir, 'events.json'), JSON.stringify(events, null, 2))
  process.exitCode = reproduced ? 2 : 0
} finally {
  await browser.close()
  await fs.writeFile(path.join(outDir, 'events.json'), JSON.stringify(events, null, 2)).catch(() => {})
}
