/**
 * App blocks in assistant replies, end to end through the real app frame,
 * AppCanvas and transcript renderer, with the in-repo fixture app
 * tests/fixtures/app-block-session.jsx (applied for real; only the chat
 * transcript is mocked).
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/app-blocks.spec.mjs
 */
import { readFileSync } from 'node:fs'
import { expect, test } from '@playwright/test'
import { applyApp } from './app-source.mjs'
import { runtimeSnapshot } from './_chatTestPrerequisites.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const CHAT_ID = '70000000-0000-4000-8000-00000000a7b1'
const FIXTURE = readFileSync(new URL('./fixtures/app-block-session.jsx', import.meta.url), 'utf8')
const PASSIVE = { 'chat.blocks.passive': { version: 1 } }

test.use({ serviceWorkers: 'block' })

const fence = value => '```mobius-app\n' + JSON.stringify(value) + '\n```'

async function ownerToken(page) {
  await page.goto(`${BASE}/shell/`, { waitUntil: 'domcontentloaded' })
  const token = await page.evaluate(() => localStorage.getItem('token'))
  expect(token).toBeTruthy()
  return token
}

async function installFixtures(page, request) {
  const token = await ownerToken(page)
  const session = await applyApp(request, token, { slug: 'e2e-block-session', name: 'Block Session',
    jsxSource: FIXTURE, capabilities: PASSIVE })
  // Same admitted capability, but the module does not export appBlockSessions.
  const plain = await applyApp(request, token, { slug: 'e2e-block-plain', name: 'Block Plain',
    jsxSource: FIXTURE.replace('export const appBlockSessions = true', ''), capabilities: PASSIVE })
  expect(session.app.passive_block_module_digest).toMatch(/^[a-f0-9]{64}$/)
  return { session: session.app, plain: plain.app }
}

async function openTranscript(page, { assistant, user = 'Show me' }) {
  await page.addInitScript(chatId => localStorage.setItem('moebius_active_chat', chatId), CHAT_ID)
  await page.route(/\/api\/chats(?:\?.*)?$/, route => route.request().method() !== 'GET' ? route.fallback()
    : route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([{
      id: CHAT_ID, title: 'App block fixture', created_at: '2026-10-23T00:00:00Z', updated_at: '2026-10-23T00:00:00Z',
      activity_at: '2026-10-23T00:00:00Z', pinned_at: null, created_by_app_id: null, has_messages: true, running: false,
    }]) }))
  await page.route(new RegExp(`/api/chats/${CHAT_ID}(?:\\?.*)?$`), route => route.request().method() !== 'GET'
    ? route.fallback()
    : route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      messages: [
        { role: 'user', content: user, ts: 1700000000000, blocks: [] },
        { role: 'assistant', content: assistant, ts: 1700000000001, blocks: [] },
      ],
      total: 2, offset: 0, ...runtimeSnapshot(),
    }) }))
  await page.route(/\/api\/chats\/[0-9a-f-]+\/stream$/, route => route.fulfill({ status: 204, body: '' }))
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  return page.locator('[data-chat-surface="painted"]')
}

/** The hidden session document, located through its own log element. */
async function sessionFrame(page) {
  let found = null
  await expect.poll(async () => {
    for (const frame of page.frames()) {
      if (await frame.locator('#block-log').count().catch(() => 0)) { found = frame; return true }
    }
    return false
  }).toBe(true)
  return found
}

test('a static block renders only in the reply and opens the app view on request', async ({ page, request }) => {
  const { session } = await installFixtures(page, request)
  const block = { app: 'e2e-block-session', intent: 'open:item-7', title: 'Fixture item',
    facts: [{ label: 'Repository', value: 'fixture/repo' }], badges: [{ label: 'Ready', tone: 'success' }] }
  const chat = await openTranscript(page, { assistant: fence(block), user: fence({ ...block, title: 'Owner pasted' }) })
  const rendered = chat.locator('.md-app-block')
  await expect(rendered).toHaveCount(1)
  await expect(rendered).toContainText('Fixture item')
  await expect(chat.locator('pre').filter({ hasText: 'Owner pasted' })).toHaveCount(1)
  await expect(rendered.locator('iframe')).toHaveCount(0)
  await rendered.getByRole('button', { name: 'Show details here' }).click()
  const view = page.frameLocator('.md-app-block__view iframe.canvas--live')
  await expect(view.locator('#fixture-intent')).toHaveText('open:item-7')
  // A transcript view is never the shell's navigation target for its app.
  await expect(page.locator(`iframe[data-app-id="${session.id}"]`)).toHaveCount(0)
})

test('a passive session drives an in-place Confirm through the real frame protocol', async ({ page, request }) => {
  await installFixtures(page, request)
  const chat = await openTranscript(page, { assistant: fence({ app: 'e2e-block-session', intent: 'open:batch',
    title: 'Ready to send', interaction: 'inline', badges: [{ label: 'Saved claim', tone: 'success' }],
    action: { label: 'Send all', intent: 'send-batch:sel-1' } }) })
  const block = chat.locator('.md-app-block')
  const action = block.locator('footer .md-app-block__action')
  await expect(action).toHaveText('Send all')
  const box = await action.boundingBox()
  const frame = await sessionFrame(page)
  const log = frame.locator('#block-log')
  const sessionId = await log.getAttribute('data-session')
  const generation = Number(await log.getAttribute('data-generation'))
  expect(sessionId).toBeTruthy()

  // The click that would follow a double-click: the moment the button reads
  // Confirm, it is disabled (not yet armed) and a click sends nothing.
  // A page-side observer clicks in the same task Confirm appears, so runner
  // speed can never turn this into a deliberate click after arming.
  const drawn = block.evaluate(section => new Promise(resolve => {
    const observer = new MutationObserver(() => {
      const button = section.querySelector('footer .md-app-block__action')
      if (button?.textContent !== 'Confirm') return
      observer.disconnect()
      const disabled = button.disabled
      button.click()
      resolve(disabled)
    })
    observer.observe(section, { subtree: true, childList: true, characterData: true })
  }))
  await action.click()
  const confirm = block.locator('footer .md-app-block__action')
  expect(await drawn).toBe(true)
  await expect(confirm).toHaveText('Confirm')
  await expect(block.getByRole('button', { name: 'Not now' })).toBeVisible()
  expect((await log.textContent()).split('\n').filter(line => line.endsWith(' confirm'))).toHaveLength(0)
  await expect(confirm).toBeEnabled()
  // Same size, same place: Confirm replaces the action in its slot.
  const confirmBox = await confirm.boundingBox()
  expect(Math.round(confirmBox.width)).toBe(Math.round(box.width))
  expect(Math.round(confirmBox.height)).toBe(Math.round(box.height))
  expect(Math.abs(confirmBox.x + confirmBox.width - (box.x + box.width))).toBeLessThan(2)
  await expect(block.locator('.md-app-block__confirmation')).toContainText('fixture/repo: Change one')
  await expect(block.locator('.md-app-block__confirmation')).toContainText('The list below comes from Block Session')
  await expect(block.locator('.md-app-block__badge')).toHaveCount(0)

  // Foreign or stale-generation messages never reach the app's own listener.
  await page.evaluate(({ sessionId, generation }) => {
    const iframe = [...document.querySelectorAll('.md-app-block__session-host iframe')][0]
    for (const message of [
      { type: 'moebius:app-block-action', sessionId: 'foreign', generation, key: 'send-batch:sel-1', event: 'confirm', nonce: 'f1' },
      { type: 'moebius:app-block-action', sessionId, generation: generation - 1, key: 'send-batch:sel-1', event: 'confirm', nonce: 'f2' },
    ]) iframe.contentWindow.postMessage(message, '*')
  }, { sessionId, generation })

  // Two clicks in the same task, before any acknowledgement can arrive,
  // send exactly one Confirm.
  await confirm.evaluate(button => { button.click(); button.click() })
  await expect(block.locator('.md-app-block__summary')).toHaveText('Sent 1')
  await expect(block.locator('footer .md-app-block__action')).toHaveText('Send all')
  await expect(block.locator('footer .md-app-block__status')).toHaveText('Sent')
  const lines = (await log.textContent()).split('\n')
  expect(lines.filter(line => line.endsWith(' confirm'))).toHaveLength(1)
  expect(lines.some(line => line.includes('foreign'))).toBe(false)
  expect(lines.every(line => !line.includes('moebius:app-block-action') || line.includes(` ${generation} `))).toBe(true)

  // The hidden document's navigation request was refused.
  expect(page.url()).not.toContain('fixture-hidden-navigation')
})

test('with the action at the bottom edge, Confirm arms only after the list end has been on screen', async ({ page, request }) => {
  await page.setViewportSize({ width: 390, height: 700 })
  await installFixtures(page, request)
  const chat = await openTranscript(page, { assistant: fence({ app: 'e2e-block-session', intent: 'open:batch',
    title: 'Ready to send', interaction: 'inline', action: { label: 'Send all', intent: 'send-batch:sel-1' } }) })
  const block = chat.locator('.md-app-block')
  const action = block.locator('footer .md-app-block__action')
  await expect(action).toHaveText('Send all')
  await action.evaluate(button => button.scrollIntoView({ block: 'end' }))
  await action.click()
  const confirm = block.locator('footer .md-app-block__action')
  await expect(confirm).toHaveText('Confirm')
  const listEnd = block.locator('.md-app-block__list-end')
  expect(await listEnd.evaluate(node => node.getBoundingClientRect().top > window.innerHeight)).toBe(true)
  // Long past the delay, the unseen list keeps Confirm disabled.
  await page.waitForTimeout(1200)
  await expect(confirm).toBeDisabled()
  // Scroll through the list to its end, then back up to Confirm.
  await listEnd.scrollIntoViewIfNeeded()
  await confirm.evaluate(button => button.scrollIntoView({ block: 'end' }))
  await expect(confirm).toBeEnabled()
  await confirm.click()
  await expect(block.locator('.md-app-block__summary')).toHaveText('Sent 1')
})

test('a list replaced after Confirm armed must have its own end seen (no borrowed intersection)', async ({ page, request }) => {
  await page.setViewportSize({ width: 390, height: 700 })
  await installFixtures(page, request)
  const chat = await openTranscript(page, { assistant: fence({ app: 'e2e-block-session', intent: 'open:swap',
    title: 'Swapping list', interaction: 'inline', action: { label: 'Send all', intent: 'swap:sel-1' } }) })
  const block = chat.locator('.md-app-block')
  const action = block.locator('footer .md-app-block__action')
  await expect(action).toHaveText('Send all')
  await action.click()
  const confirm = block.locator('footer .md-app-block__action')
  // One target: its end is on screen, so Confirm arms.
  await expect(confirm).toBeEnabled()
  // The app replaces it with 256 targets while the old end was visible.
  await expect(block.locator('.md-app-block__confirmation li')).toHaveCount(256)
  expect(await block.locator('.md-app-block__list-end').evaluate(node => node.getBoundingClientRect().top > window.innerHeight)).toBe(true)
  await page.waitForTimeout(900)
  await expect(confirm).toBeDisabled()
  const frame = await sessionFrame(page)
  await confirm.evaluate(button => button.click())
  await page.waitForTimeout(200)
  expect((await frame.locator('#block-log').textContent()).split('\n').filter(line => line.endsWith(' confirm'))).toHaveLength(0)
  // Scrolling to the new end and back arms it.
  await block.locator('.md-app-block__list-end').scrollIntoViewIfNeeded()
  await confirm.evaluate(button => button.scrollIntoView({ block: 'end' }))
  await expect(confirm).toBeEnabled()
})

test('a document reload discards idle controls until the new document reports', async ({ page, request }) => {
  await installFixtures(page, request)
  const chat = await openTranscript(page, { assistant: fence({ app: 'e2e-block-session', intent: 'open:one',
    title: 'Reload me', interaction: 'inline', action: { label: 'Send', intent: 'send:one' } }) })
  const block = chat.locator('.md-app-block')
  await expect(block.locator('footer .md-app-block__action')).toHaveText('Send')
  // An idle Confirm belongs to its document; a reload must not carry it over.
  await block.locator('footer .md-app-block__action').click()
  await expect(block.locator('footer .md-app-block__action')).toHaveText('Confirm')
  const frame = await sessionFrame(page)
  const before = Number(await frame.locator('#block-log').getAttribute('data-generation'))
  await frame.evaluate(() => location.reload())
  await expect(block.getByRole('button', { name: 'Confirm' })).toHaveCount(0)
  const next = await sessionFrame(page)
  await expect.poll(async () => Number(await next.locator('#block-log').getAttribute('data-generation').catch(() => before)))
    .toBeGreaterThan(before)
  await expect(block.locator('footer .md-app-block__action')).toHaveText('Send')
  await block.locator('footer .md-app-block__action').click()
  await expect(block.locator('footer .md-app-block__action')).toHaveText('Confirm')
})

test('an admitted module without session support falls back to a fresh visible view', async ({ page, request }) => {
  await installFixtures(page, request)
  const chat = await openTranscript(page, { assistant: fence({ app: 'e2e-block-plain', intent: 'open:plain',
    title: 'Plain app', interaction: 'inline', action: { label: 'Review', intent: 'review:plain' } }) })
  const block = chat.locator('.md-app-block')
  const action = block.getByRole('button', { name: 'Review' })
  await expect(action).toBeVisible()
  await expect(block.locator('.md-app-block__session-host')).toHaveCount(0)
  await action.click()
  const view = page.frameLocator('.md-app-block__view iframe.canvas--live')
  await expect(view.locator('#fixture-intent')).toHaveText('review:plain')
})
