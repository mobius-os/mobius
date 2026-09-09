/** Saved edits synchronize across independent project guests without GitHub.
 * Keep this separate from keystroke co-editing: unsaved drafts stay local and
 * stale saves must never overwrite a collaborator's newer revision.
 */
import { expect, test } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test('two editors share saves, preserve conflicting drafts, and deny viewer writes', async ({ page, request, browser }) => {
  await page.goto(`${BASE}/shell/`, { waitUntil: 'domcontentloaded' })
  const token = await page.evaluate(() => localStorage.getItem('token'))
  expect(token).toBeTruthy()
  const headers = { Authorization: `Bearer ${token}` }
  const created = await request.post(`${BASE}/api/projects`, {
    headers, data: { name: `Saved sharing fixture ${Date.now()}`, template_id: 'blank' },
  })
  expect(created.ok()).toBeTruthy()
  const project = await created.json()
  const url = `${BASE}/api/projects/${project.id}`
  const contexts = []
  const deployments = []
  const githubCalls = []
  try {
    const seeded = await request.put(`${url}/file?path=notes.txt`, {
      headers, data: { content: 'original\n', expected_revision: null },
    })
    expect(seeded.ok()).toBeTruthy()
    const initialized = await request.post(`${url}/git/init`, { headers })
    expect(initialized.ok()).toBeTruthy()
    async function guest(name, role) {
      const invited = await request.post(`${url}/invites`, {
        headers, data: { invitee_name: name, role },
      })
      expect(invited.ok()).toBeTruthy()
      const { join_url: joinUrl } = await invited.json()
      const context = await browser.newContext({ storageState: { cookies: [], origins: [] } })
      contexts.push(context)
      const guestPage = await context.newPage()
      guestPage.on('request', req => {
        if (/\/apps\/apply(?:\?|$)|\/apply-source(?:\?|$)|\/artifacts\/[^/]+\/build(?:\?|$)/.test(req.url()) && req.method() === 'POST') deployments.push(req.url())
        if (/\/git\/(?:fetch|pull|push)|api\.github\.com/.test(req.url())) githubCalls.push(req.url())
      })
      // Use the current test origin, not an externally configured public URL.
      const join = new URL(joinUrl)
      await guestPage.goto(`${BASE}${join.pathname}${join.search}${join.hash}`)
      await guestPage.getByLabel('Your name', { exact: true }).fill(name)
      await guestPage.getByRole('button', { name: 'Join project', exact: true }).click()
      await expect(guestPage.locator('.project-finder')).toBeVisible()
      await guestPage.locator('.project-finder__row-main').filter({ hasText: 'notes.txt' }).click()
      await expect(guestPage.locator('.project-finder__pane-title')).toContainText('notes.txt')
      return guestPage
    }
    const alice = await guest('Editor Alice', 'editor')
    const bob = await guest('Editor Bob', 'editor')
    await alice.getByRole('button', { name: 'Edit', exact: true }).click()
    await alice.getByRole('textbox', { name: 'Edit notes.txt', exact: true }).fill('Alice shared save\n')
    await alice.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(alice.getByRole('button', { name: 'Edit', exact: true })).toBeVisible()
    // Bob is a different browser context: this can only arrive through the
    // real changes feed and revision read, not shared query cache or events.
    await expect(bob.locator('.project-finder__surface')).toContainText('Alice shared save', { timeout: 15000 })
    await bob.getByRole('button', { name: 'Edit', exact: true }).click()
    await bob.getByRole('textbox', { name: 'Edit notes.txt', exact: true }).fill('Bob unsaved draft\n')
    await alice.getByRole('button', { name: 'Edit', exact: true }).click()
    await alice.getByRole('textbox', { name: 'Edit notes.txt', exact: true }).fill('Alice newer save\n')
    await alice.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(bob.getByText('This file changed elsewhere', { exact: true })).toBeVisible({ timeout: 15000 })
    await expect(bob.getByRole('textbox', { name: 'Edit notes.txt', exact: true })).toHaveValue('Bob unsaved draft\n')
    await bob.getByRole('button', { name: 'Changes', exact: true }).click()
    await expect(bob.getByText('Showing saved changes. Your unsaved draft is preserved in Code.')).toBeVisible()
    await bob.getByRole('button', { name: /^Code/ }).click()
    await expect(bob.getByRole('textbox', { name: 'Edit notes.txt', exact: true })).toHaveValue('Bob unsaved draft\n')
    const staleSave = bob.waitForResponse(response => response.url().includes('/file?path=notes.txt') && response.request().method() === 'PUT')
    await bob.getByRole('button', { name: 'Save', exact: true }).click()
    expect((await staleSave).status()).toBe(409)
    await expect(bob.getByRole('textbox', { name: 'Edit notes.txt', exact: true })).toHaveValue('Bob unsaved draft\n')
    const saved = await request.get(`${url}/file?path=notes.txt`, { headers })
    expect((await saved.json()).content).toBe('Alice newer save\n')

    const viewer = await guest('Read only Robin', 'viewer')
    await expect(viewer.getByRole('button', { name: 'Edit', exact: true })).toHaveCount(0)
    const denied = await viewer.evaluate(async ({ projectId, apiUrl }) => {
      const guestToken = sessionStorage.getItem(`mobius:project-session:${projectId}`)
      const response = await fetch(`${apiUrl}/file?path=blocked.txt`, {
        method: 'PUT', headers: { Authorization: `Bearer ${guestToken}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ content: 'must not save', expected_revision: null }),
      })
      return response.status
    }, { projectId: project.id, apiUrl: url })
    expect(denied).toBe(403)
    expect(deployments).toEqual([])
    expect(githubCalls).toEqual([])
  } finally {
    for (const context of contexts) await context.close()
    const removed = await request.delete(url, { headers })
    expect(removed.status()).toBe(204)
  }
})
