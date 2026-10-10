/** Owner filing is a UI move, not a chat close or transcript deletion. */
import { test, expect } from '@playwright/test'
import { attachCleanup, registerCreatedChats, workerChatTitle } from './_chatTracker.mjs'
import { mockDeliveryReady } from './_chatTestPrerequisites.mjs'
import { waitForChatShell } from './_chatSession.mjs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
test.use({ serviceWorkers: 'block' })
attachCleanup()

test('Archive, Archived, open-chat Restore and Undo keep the same transcript and tab', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 })
  await mockDeliveryReady(page)
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await waitForChatShell(page)
  const token = await page.evaluate(() => localStorage.getItem('token'))
  const title = workerChatTitle(test.info().workerIndex, 'archive-ui')
  const original = [
    { role: 'user', content: 'Keep this archived history', ts: 1700000000000 },
    { role: 'assistant', content: 'The answer remains here.', ts: 1700000000001 },
  ]
  // Create settled history directly; this test never starts an agent/provider.
  const created = await page.request.post(`${BASE}/api/chats`, {
    headers: { Authorization: `Bearer ${token}` },
    data: { title, messages: original },
  })
  expect(created.ok()).toBe(true)
  const { id } = await created.json()
  registerCreatedChats(test.info().workerIndex, id)
  const read = async () => {
    const response = await page.request.get(`${BASE}/api/chats/${id}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(response.ok()).toBe(true)
    return response.json()
  }
  const readRow = async () => {
    const response = await page.request.get(`${BASE}/api/chats?ids=${encodeURIComponent(id)}`, {
      headers: { Authorization: `Bearer ${token}` },
    })
    expect(response.ok()).toBe(true)
    const rows = await response.json()
    expect(rows).toHaveLength(1)
    expect(Object.hasOwn(rows[0], 'archived_at')).toBe(true)
    return rows[0]
  }
  await page.goto(`${BASE}/shell/?chat=${id}`, { waitUntil: 'domcontentloaded' })
  const surface = page.locator('[data-chat-surface="painted"]')
  const history = async () => {
    await expect(surface.locator('.chat__msg--user')).toContainText(original[0].content)
    await expect(surface.locator('.chat__msg--assistant')).toContainText(original[1].content)
    expect((await read()).messages.map(message => message.content)).toEqual(original.map(message => message.content))
  }
  await history()
  const originalUrl = page.url()
  const toggle = page.getByRole('button', { name: 'Toggle navigation' })
  if (await toggle.getAttribute('aria-expanded') !== 'true') await toggle.click()
  const recents = page.getByRole('tab', { name: 'Recents' })
  const archived = page.getByRole('tab', { name: 'Archived' })
  await recents.click()
  const row = page.locator(`[data-drawer-key="chat:${id}"]`)
  await expect(row).toBeVisible()
  await row.click({ button: 'right' })
  const menu = page.getByRole('menu')
  expect(await menu.locator('[role="menuitem"], [role="separator"]').evaluateAll(
    elements => elements.map(element => element.getAttribute('role') === 'separator'
      ? 'separator' : element.textContent.trim()),
  )).toEqual(['Pin', 'Copy name', 'Rename', 'separator', 'Archive', 'Delete'])
  await expect(menu.getByRole('menuitem', { name: 'Archive', exact: true }).locator('svg')).toHaveCount(0)
  await page.getByRole('menuitem', { name: 'Archive', exact: true }).click()
  await expect(surface.getByRole('region', { name: 'Archived chat' })).toBeVisible()
  await expect.poll(async () => (await readRow()).archived_at).not.toBeNull()
  await expect(row).toHaveCount(0)
  await archived.click()
  await expect(row).toBeVisible()
  await expect(row).toHaveAttribute('aria-current', 'page')
  await history()
  expect(page.url()).toBe(originalUrl)

  await surface.getByRole('button', { name: 'Restore', exact: true }).click()
  await expect(surface.getByRole('region', { name: 'Archived chat' })).toHaveCount(0)
  await expect.poll(async () => (await readRow()).archived_at).toBeNull()
  await recents.click()
  await expect(row).toBeVisible()
  await history()
  expect(page.url()).toBe(originalUrl)

  await row.click({ button: 'right' })
  await page.getByRole('menuitem', { name: 'Archive', exact: true }).click()
  await expect(surface.getByRole('region', { name: 'Archived chat' })).toBeVisible()
  const preview = page.getByRole('region', { name: 'Notifications', exact: true })
  await expect(preview).toHaveCount(0)
  await page.getByRole('button', { name: /^Notifications(?:,|$)/ }).click()
  const notice = preview.locator('.notifications__row-item').filter({
    hasText: 'Chat archived',
    has: page.getByRole('button', { name: 'Undo', exact: true }),
  })
  await expect(notice).toHaveCount(1)
  await notice.getByRole('button', { name: 'Undo', exact: true }).click()
  await expect(notice).toHaveCount(0)
  await expect(preview.getByText('Undone', { exact: true })).toBeVisible()
  await page.getByRole('button', { name: 'Close notifications', exact: true }).click()
  await expect(surface.getByRole('region', { name: 'Archived chat' })).toHaveCount(0)
  await expect.poll(async () => (await readRow()).archived_at).toBeNull()
  await expect(row).toBeVisible()
  await history()
  expect(page.url()).toBe(originalUrl)
})
