import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test.use({ serviceWorkers: 'block' })

function connector(id, overrides = {}) {
  return {
    id,
    slug: `server_${id}`,
    name: `Server ${id}`,
    url: `https://mcp${id}.example/mcp`,
    enabled: true,
    has_auth: false,
    tool_count: 2,
    tools: [{ name: 'lookup' }, { name: 'summarize' }],
    est_tokens: 420,
    status: 'ok',
    status_detail: null,
    ...overrides,
  }
}

async function openConnections(page) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' })
  await page.waitForFunction(
    () => !!(document.querySelector('.chat__empty-wrap')
      || document.querySelector('.chat__scroll')
      || document.querySelector('.chat__form')),
    { timeout: 10000 },
  )
  const navigationToggle = page.getByLabel('Toggle navigation')
  if (await navigationToggle.getAttribute('aria-expanded') !== 'true') {
    await navigationToggle.click()
  }
  await page.getByRole('button', { name: 'Settings', exact: true }).click()
  await expect(page.locator('#settings-connections')).toBeVisible()
}

test('connection actions preserve secrets, ordering, and keyboard focus contracts', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 })
  const rows = [connector(1), connector(2)]
  let addGateResolve
  const addGate = new Promise(resolve => { addGateResolve = resolve })

  await page.route('**/api/connectors**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    const match = url.pathname.match(/^\/api\/connectors\/(\d+)(?:\/(refresh))?$/)
    if (request.method() === 'GET' && url.pathname === '/api/connectors') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ connectors: rows }) })
    }
    if (request.method() === 'POST' && url.pathname === '/api/connectors') {
      await addGate
      const created = connector(3, { name: request.postDataJSON().name || 'Server 3', has_auth: true })
      rows.push(created)
      return route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(created) })
    }
    const id = Number(match?.[1])
    const row = rows.find(item => item.id === id)
    if (request.method() === 'PATCH' && row) {
      Object.assign(row, request.postDataJSON())
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(row) })
    }
    if (request.method() === 'POST' && match?.[2] === 'refresh' && row) {
      row.tool_count = 3
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(row) })
    }
    if (request.method() === 'DELETE' && row) {
      rows.splice(rows.indexOf(row), 1)
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ok: true }) })
    }
    return route.fallback()
  })

  await openConnections(page)
  await page.getByRole('button', { name: 'Add connection' }).click()
  await page.getByRole('button', { name: 'Add API key' }).click()
  await page.getByLabel('API key', { exact: true }).fill('private-value')
  await page.getByRole('button', { name: 'Remove API key' }).click()
  await page.getByRole('button', { name: 'Add API key' }).click()
  await expect(page.getByLabel('API key', { exact: true })).toHaveValue('')

  await page.getByLabel('Streamable HTTP endpoint').fill('https://new.example/mcp')
  await page.getByLabel('Name optional').fill('New server')
  await page.getByLabel('API key', { exact: true }).fill('replacement-value')
  await page.getByRole('button', { name: 'Check and add' }).click()
  await expect(page.getByRole('button', { name: 'Cancel' })).toBeDisabled()
  addGateResolve()
  await expect(page.getByText('New server', { exact: true })).toBeVisible()

  const firstRemove = page.getByRole('button', { name: 'Remove Server 1' })
  await firstRemove.focus()
  await firstRemove.click()
  const confirm = page.getByRole('button', { name: 'Remove', exact: true })
  await expect(confirm).toBeFocused()
  await page.getByRole('button', { name: 'Keep' }).click()
  await expect(firstRemove).toBeFocused()
  await firstRemove.click()
  await confirm.click()
  await expect(page.getByRole('button', { name: 'Remove Server 2' })).toBeFocused()

  const toggle = page.getByRole('switch', { name: 'Server 2 available to agents' })
  await toggle.click()
  await expect(toggle).toHaveAttribute('aria-checked', 'false')
  await page.getByRole('button', { name: 'Re-check Server 2' }).click()
  await expect(page.getByText(/3 tools/)).toBeVisible()
})

test('restart-unavailable state retries into the empty connection surface', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 })
  let unavailable = true
  await page.route('**/api/connectors', route => {
    if (unavailable) {
      unavailable = false
      return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"restart"}' })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{"connectors":[]}' })
  })
  await openConnections(page)
  await expect(page.getByText(/finish setting up after the server restarts/)).toBeVisible()
  await page.getByRole('button', { name: 'Check again' }).click()
  await expect(page.getByText('No custom MCP connections yet.')).toBeVisible()
})

test('long connection details stay inside the narrow Settings pane', async ({ page }) => {
  await page.setViewportSize({ width: 280, height: 720 })
  const long = 'connection'.repeat(20)
  await page.route('**/api/connectors', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ connectors: [connector(1, {
      name: long,
      status: 'error',
      status_detail: long,
    })] }),
  }))
  await openConnections(page)
  const section = page.locator('#settings-connections')
  await expect.poll(() => section.evaluate(element => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }))).toEqual(expect.objectContaining({ client: expect.any(Number) }))
  const overflow = await section.evaluate(element => element.scrollWidth - element.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
})
