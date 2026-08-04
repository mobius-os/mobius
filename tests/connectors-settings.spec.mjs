import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test.use({ serviceWorkers: 'block' })

function connector(id, overrides = {}) {
  return {
    id,
    generation: `generation-${id}`,
    name: `Server ${id}`,
    url: `https://mcp${id}.example/mcp`,
    enabled: true,
    has_auth: false,
    tool_count: 2,
    status: 'ok',
    status_detail: null,
    ...overrides,
  }
}

function expectGeneration(request, row) {
  expect(request.headers()['x-mobius-connector-generation']).toBe(row.generation)
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

test('connection actions clear secrets, serialize changes, and restore focus', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 })
  const rows = [connector(1), connector(2)]
  let addedPayload
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
      addedPayload = request.postDataJSON()
      const created = connector(3, { name: addedPayload.name || 'Server 3', has_auth: true })
      rows.push(created)
      return route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify(created) })
    }
    const id = Number(match?.[1])
    const row = rows.find(item => item.id === id)
    if (request.method() === 'PATCH' && row) {
      expectGeneration(request, row)
      const payload = request.postDataJSON()
      if (row.enabled && payload.enabled === false) {
        row.generation = `${row.generation}-revoked`
      }
      Object.assign(row, payload)
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(row) })
    }
    if (request.method() === 'POST' && match?.[2] === 'refresh' && row) {
      expectGeneration(request, row)
      row.tool_count = 3
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(row) })
    }
    if (request.method() === 'DELETE' && row) {
      expectGeneration(request, row)
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
  await expect(page.getByLabel('Streamable HTTP endpoint')).toBeDisabled()
  await expect(page.getByRole('switch', { name: 'Server 1 available to agents' })).toBeDisabled()
  addGateResolve()
  await expect(page.getByText('New server', { exact: true })).toBeVisible()
  expect(addedPayload.auth_value).toBe('replacement-value')
  await expect(page.getByRole('button', { name: 'Add connection' })).toBeFocused()

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
  await expect(toggle).toBeFocused()
  const refresh = page.getByRole('button', { name: 'Re-check Server 2' })
  await refresh.click()
  await expect(page.getByText(/3 tools/)).toBeVisible()
  await expect(refresh).toBeFocused()

  await page.getByRole('button', { name: 'Remove New server' }).click()
  await page.getByRole('button', { name: 'Remove', exact: true }).click()
  await expect(page.getByRole('button', { name: 'Remove Server 2' })).toBeFocused()
  await page.getByRole('button', { name: 'Add connection' }).click()
  await page.getByRole('button', { name: 'Remove Server 2' }).click()
  await page.getByRole('button', { name: 'Remove', exact: true }).click()
  await expect(page.getByLabel('Streamable HTTP endpoint')).toBeFocused()
})

test('a replacement cannot inherit another connection’s removal confirmation', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 })
  const rows = [connector(1), connector(2)]

  await page.route('**/api/connectors**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() === 'GET' && url.pathname === '/api/connectors') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ connectors: rows }),
      })
    }
    if (request.method() === 'POST' && url.pathname === '/api/connectors/2/refresh') {
      expectGeneration(request, rows[1])
      rows[0] = connector(1, {
        generation: 'replacement-generation',
        name: 'Replacement server',
      })
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(rows[1]),
      })
    }
    return route.fallback()
  })

  await openConnections(page)
  await page.getByRole('button', { name: 'Remove Server 1' }).click()
  await expect(page.getByRole('button', { name: 'Remove', exact: true })).toBeFocused()
  await page.getByRole('button', { name: 'Re-check Server 2' }).click()
  await expect(page.getByText('Replacement server', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Remove', exact: true })).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Remove Replacement server' })).toBeFocused()
})

test('a failed initial load retries into the empty connection surface', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 })
  let unavailable = true
  await page.route('**/api/connectors', route => {
    if (unavailable) {
      return route.fulfill({ status: 503, contentType: 'application/json', body: '{"detail":"restart"}' })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: '{"connectors":[]}' })
  })
  await openConnections(page)
  await expect(page.getByText(/Could not load connections/)).toBeVisible()
  unavailable = false
  await page.getByRole('button', { name: 'Retry' }).click()
  await expect(page.getByText('No custom MCP connections yet.')).toBeVisible()
})

test('a background refresh failure keeps the last usable list', async ({ page }) => {
  await page.setViewportSize({ width: 900, height: 800 })
  const rows = [connector(1)]
  let listReads = 0
  await page.route('**/api/connectors**', async route => {
    const request = route.request()
    const url = new URL(request.url())
    if (request.method() === 'GET' && url.pathname === '/api/connectors') {
      listReads += 1
      if (listReads > 1) {
        return route.fulfill({ status: 502, contentType: 'application/json', body: '{"detail":"offline"}' })
      }
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ connectors: rows }),
      })
    }
    if (request.method() === 'POST' && url.pathname === '/api/connectors/1/refresh') {
      expectGeneration(request, rows[0])
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify(rows[0]),
      })
    }
    return route.fallback()
  })

  await openConnections(page)
  await page.getByRole('button', { name: 'Re-check Server 1' }).click()
  await expect(page.getByText('Server 1', { exact: true })).toBeVisible()
  await expect(page.getByText(/saved list could not be refreshed/)).toBeVisible()
})

test('long connection details stay inside the narrow Settings pane', async ({ page }) => {
  await page.setViewportSize({ width: 280, height: 720 })
  const longName = 'n'.repeat(128)
  const longError = 'connection'.repeat(20)
  const urlSecret = 'must-not-render'
  await page.route('**/api/connectors', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ connectors: [connector(1, {
      name: longName,
      url: `https://mcp1.example/mcp?access_token=${urlSecret}`,
      status: 'error',
      status_detail: longError,
    })] }),
  }))
  await openConnections(page)
  const section = page.locator('#settings-connections')
  await expect(section).toContainText('On · Needs attention')
  await expect(section).not.toContainText(urlSecret)
  await expect.poll(() => section.evaluate(element => ({
    client: element.clientWidth,
    scroll: element.scrollWidth,
  }))).toEqual(expect.objectContaining({ client: expect.any(Number) }))
  const overflow = await section.evaluate(element => element.scrollWidth - element.clientWidth)
  expect(overflow).toBeLessThanOrEqual(1)
})
