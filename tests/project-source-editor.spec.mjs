/** The artifact shortcut edits source directly without silently discarding drafts. */
import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test('artifact source opens in Code, preserves drafts, and supports Save and Cancel', async ({ page, request }) => {
  await page.goto(`${BASE}/shell/`, { waitUntil: 'domcontentloaded' })
  const token = await page.evaluate(() => localStorage.getItem('token'))
  expect(token).toBeTruthy()
  const headers = { Authorization: `Bearer ${token}` }
  const created = await request.post(`${BASE}/api/projects`, {
    headers, data: { name: `Source editor fixture ${Date.now()}`, template_id: 'blank' },
  })
  expect(created.ok()).toBeTruthy()
  const project = await created.json()
  const url = `${BASE}/api/projects/${project.id}`
  try {
    for (const [path, content] of [['index.html', '<h1>Original</h1>'], ['other.md', '# Other']]) {
      const seeded = await request.put(`${url}/file?path=${path}`, { headers, data: { content, expected_revision: null } })
      expect(seeded.ok()).toBeTruthy()
    }
    await page.route(`**/api/projects/${project.id}/artifacts`, async route => {
      if (route.request().method() !== 'GET') return route.continue()
      await route.fulfill({ json: [
        { id: 'first', name: 'First', source: 'index.html', status: 'ready' },
        { id: 'second', name: 'Second', source: 'other.md', status: 'ready' },
      ] })
    })
    await page.route(`**/api/projects/${project.id}/artifacts/*/build`, route => route.fulfill({ json: {} }))
    await page.goto(`${BASE}/shell/?project=${project.id}`)
    await page.getByRole('button', { name: 'Edit source of First' }).click()
    const editor = page.getByRole('textbox', { name: 'Edit index.html' })
    await expect(editor).toHaveValue('<h1>Original</h1>')
    await expect(page.getByRole('button', { name: 'Code', exact: true })).toHaveAttribute('aria-pressed', 'true')
    await expect(page.getByText('Editing · no changes', { exact: true })).toBeVisible()
    await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeDisabled()
    await editor.fill('<h1>Draft</h1>')
    await expect(page.getByText('Editing · unsaved changes', { exact: true })).toBeVisible()

    page.once('dialog', dialog => dialog.dismiss())
    await page.getByRole('button', { name: 'Edit source of Second' }).click()
    await expect(editor).toHaveValue('<h1>Draft</h1>')
    page.once('dialog', dialog => dialog.dismiss())
    await page.getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(editor).toHaveValue('<h1>Draft</h1>')
    page.once('dialog', dialog => dialog.accept())
    await page.getByRole('button', { name: 'Cancel', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Edit', exact: true })).toBeVisible()
    await expect(page.locator('.project-finder__surface')).toContainText('<h1>Original</h1>')

    await page.getByRole('button', { name: 'Edit source of First' }).click()
    await editor.fill('<h1>Saved</h1>')
    await page.getByRole('button', { name: 'Save', exact: true }).click()
    await expect(page.getByRole('button', { name: 'Edit', exact: true })).toBeVisible()
    const saved = await request.get(`${url}/file?path=index.html`, { headers })
    expect((await saved.json()).content).toBe('<h1>Saved</h1>')
  } finally {
    const removed = await request.delete(url, { headers })
    expect(removed.status()).toBe(204)
  }
})

test('phone code view advertises horizontal scrolling and its editor wraps long lines', async ({ page }) => {
  const projectsCss = readFileSync(new URL('../frontend/src/components/Projects/Projects.css', import.meta.url), 'utf8')
  const editorCss = readFileSync(new URL('../frontend/src/components/Projects/ProjectSourceEditor.css', import.meta.url), 'utf8')
  await page.setViewportSize({ width: 390, height: 844 })
  await page.setContent(`<style>${projectsCss}\n${editorCss}</style>
    <div class="project-finder" style="width:360px;height:400px">
      <div class="project-finder__surface">
        <div class="project-finder__code"><p class="project-source-editor__scroll-hint">Scroll sideways to see long lines</p><div class="project-finder__code-body project-finder__code-body--numbered"><pre>${'x'.repeat(300)}</pre></div></div>
      </div>
      <div class="project-finder__surface"><textarea>${'x'.repeat(300)}</textarea></div>
    </div>`)
  await expect(page.getByText('Scroll sideways to see long lines')).toBeVisible()
  const scroller = page.locator('.project-finder__code')
  expect(await scroller.evaluate(element => element.scrollWidth > element.clientWidth)).toBeTruthy()
  const textarea = page.locator('textarea')
  expect(await textarea.evaluate(element => getComputedStyle(element).whiteSpace)).toBe('pre-wrap')
})
