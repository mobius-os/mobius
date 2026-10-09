import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'
const SCREEN_COUNT = 12

// Keep the browser's first-run requests inside this isolated test case.
test.use({ serviceWorkers: 'block' })

async function openGuide(page) {
  let completed = false
  let completions = 0

  await page.addInitScript(() => {
    if (sessionStorage.getItem('onboarding-test-started')) return
    localStorage.removeItem('mobius:walkthrough-completed')
    sessionStorage.setItem('onboarding-test-started', '1')
  })
  await page.route(/\/api\/owner\/walkthrough$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({ completed, completed_at: completed ? new Date().toISOString() : null }),
  }))
  await page.route(/\/api\/owner\/walkthrough\/complete$/, route => {
    completions += 1
    completed = true
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ completed: true, completed_at: new Date().toISOString() }),
    })
  })
  await page.route(/\/api\/auth\/providers\/status$/, route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: '{}',
  }))

  await page.goto(`${BASE}/shell/`)
  const guide = page.getByRole('dialog', { name: /./ }).filter({ has: page.locator('.wt__bars') })
  await expect(guide).toBeVisible()
  return { guide, completionCount: () => completions }
}

const CATALOG = { schema: 1, apps: [
  { id: 'memory', name: 'Memory', description: 'Remember things.', manifest_url: 'https://example.com/memory/mobius.json', raw_base: 'https://example.com/memory/' },
  { id: 'reflection', name: 'Reflection', description: 'Learn from mistakes.', manifest_url: 'https://example.com/reflection/mobius.json', raw_base: 'https://example.com/reflection/' },
] }

async function stubCatalog(page) {
  await page.route(/\/api\/proxy\?/, route => {
    const source = new URL(route.request().url()).searchParams.get('url')
    if (source?.endsWith('/catalog.json')) return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(CATALOG) })
    return route.fulfill({ status: 404, body: '' })
  })
}

async function goToScreen(guide, name) {
  await guide.getByRole('button', { name: `Go to ${name}` }).click()
}

test('a new owner can move through the guide and finish it', async ({ page }) => {
  const { guide, completionCount } = await openGuide(page)
  await expect(guide).toHaveAttribute('aria-modal', 'true')
  await expect(guide.getByRole('heading', { name: /Welcome to Möbius/ })).toBeFocused()
  await expect(guide.getByRole('button', { name: /^Go to / })).toHaveCount(SCREEN_COUNT)

  await guide.getByRole('button', { name: 'Continue' }).click()
  await expect(guide.getByRole('heading', { name: /Say what you need/ })).toBeFocused()
  await guide.getByRole('button', { name: 'Back' }).click()
  await expect(guide.getByRole('heading', { name: /Welcome to Möbius/ })).toBeVisible()

  for (let step = 1; step < SCREEN_COUNT; step += 1) await guide.getByRole('button', { name: 'Continue' }).click()
  await expect(guide.getByRole('heading', { name: /Good luck/ })).toBeVisible()
  await guide.getByRole('button', { name: 'Finish guide' }).click()
  await expect(guide).toHaveCount(0)
  await expect.poll(completionCount).toBe(1)

  const refreshedStatus = page.waitForResponse(response => new URL(response.url()).pathname === '/api/owner/walkthrough')
  await page.reload()
  await refreshedStatus
  await expect(page.locator('.shell')).toBeVisible()
  await expect(guide).toHaveCount(0)
})

test('the guide is a modal: Tab stays inside, and Escape dismisses it like the close button', async ({ page }) => {
  const { guide, completionCount } = await openGuide(page)
  for (let press = 0; press < 30; press += 1) {
    await page.keyboard.press('Tab')
    expect(await guide.evaluate(card => card.contains(document.activeElement))).toBe(true)
  }
  await page.keyboard.press('Escape')
  await expect(guide).toHaveCount(0)
  await expect.poll(completionCount).toBe(1)
})

test('Escape in the access review cancels only the review, not the guide', async ({ page }) => {
  await stubCatalog(page)
  await page.route(/\/api\/apps\/preview$/, route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ manifest: { id: 'memory' }, capability_contract: {}, capability_digest: 'd1' }) }))
  const { guide, completionCount } = await openGuide(page)
  await goToScreen(guide, 'Make it yours')
  await guide.getByRole('button', { name: 'Install Memory' }).click()
  await expect(page.getByRole('alertdialog', { name: 'Memory asks for access' })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(page.getByRole('alertdialog')).toHaveCount(0)
  await expect(guide).toBeVisible()
  expect(completionCount()).toBe(0)
})

test('mobile guide fills the screen and keeps Continue reachable', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 700 })
  const { guide } = await openGuide(page)
  await goToScreen(guide, 'Meet your agent')
  await goToScreen(guide, 'App Store')
  await expect(guide.getByRole('button', { name: 'Continue' })).toBeInViewport()
  await expect(guide.getByRole('button', { name: 'Dismiss welcome' })).toBeInViewport()
  await guide.getByRole('button', { name: 'Continue' }).click()
  await expect(guide.getByRole('heading', { name: /Collaboration apps/ })).toBeVisible()
})

test('short landscape keeps guide actions and dismissal reachable', async ({ page }) => {
  await page.setViewportSize({ width: 700, height: 360 })
  const { guide, completionCount } = await openGuide(page)
  await expect(guide.getByRole('button', { name: 'Continue' })).toBeInViewport()
  await expect(guide.getByRole('button', { name: 'Dismiss welcome' })).toBeInViewport()
  await guide.getByRole('button', { name: 'Dismiss welcome' }).click()
  await expect(guide).toHaveCount(0)
  await expect.poll(completionCount).toBe(1)
})

test('installing an app asks for access first and installs exactly what was reviewed', async ({ page }) => {
  await stubCatalog(page)
  const requests = []
  await page.route(/\/api\/apps\/(preview|install)$/, route => {
    const request = route.request()
    requests.push({ path: new URL(request.url()).pathname.split('/').pop(), body: request.postDataJSON() })
    if (request.url().endsWith('/preview')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ manifest: { id: 'memory' }, capability_contract: { data: { shared_memory: 'write' } }, capability_digest: 'digest-1' }) })
    }
    return route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ warnings: [] }) })
  })
  const { guide } = await openGuide(page)
  await goToScreen(guide, 'Make it yours')
  const install = guide.getByRole('button', { name: 'Install Memory' })
  await install.click()

  const access = page.getByRole('alertdialog', { name: 'Memory asks for access' })
  await expect(access).toBeVisible()
  await expect(access.getByText('Shared memory', { exact: true })).toBeVisible()
  await expect(access.getByRole('button', { name: 'Confirm and install' })).toBeFocused()
  // Nothing is installed while the access is being read.
  expect(requests.map(request => request.path)).toEqual(['preview'])
  // The other app cannot be started while this review is open.
  await expect(guide.getByRole('button', { name: 'Install Reflection' })).toHaveAttribute('aria-disabled', 'true')

  await access.getByRole('button', { name: 'Cancel' }).click()
  await expect(access).toHaveCount(0)
  await expect(install).toBeFocused()
  expect(requests.map(request => request.path)).toEqual(['preview'])

  await install.click()
  await page.getByRole('button', { name: 'Confirm and install' }).click()
  await expect.poll(() => requests.filter(request => request.path === 'install').length).toBe(1)
  expect(requests.at(-1).body.reviewed_capability_digest).toBe('digest-1')
})

test('an access check that returns after the owner moved on does not open a review', async ({ page }) => {
  await stubCatalog(page)
  let release
  const held = new Promise(resolve => { release = resolve })
  await page.route(/\/api\/apps\/preview$/, async route => {
    await held
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ manifest: { id: 'memory' }, capability_contract: {}, capability_digest: 'late' }) })
  })
  const { guide } = await openGuide(page)
  await goToScreen(guide, 'Make it yours')
  await guide.getByRole('button', { name: 'Install Memory' }).click()
  await guide.getByRole('button', { name: 'Continue' }).click()
  release()
  await expect(guide.getByRole('heading', { name: /Build useful/ })).toBeVisible()
  await expect(page.getByRole('alertdialog')).toHaveCount(0)
})

async function stubIdentity(page, identity) {
  let current = identity
  await page.route(/\/api\/identity$/, route => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(current) }))
  await page.route(/\/api\/identity\/profile$/, route => {
    current = { ...current, profile: { ...current.profile, handle: route.request().postDataJSON().handle } }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(current) })
  })
}

const linked = handle => ({ account_mode: 'linked', profile: { handle, display_name: 'Ada', avatar_url: null } })

test('an owner who arrives with a handle is welcomed back and can skip to agent setup', async ({ page }) => {
  await stubIdentity(page, linked('ada'))
  const { guide } = await openGuide(page)
  await expect(guide.getByRole('note')).toContainText('Welcome back, @ada.')
  // While the handle form is open, the note and the skip step aside together.
  await guide.getByRole('button', { name: 'Change', exact: true }).click()
  await expect(guide.getByRole('note')).toHaveCount(0)
  await expect(guide.getByRole('button', { name: 'Skip to agent setup' })).toHaveCount(0)
  await guide.getByRole('button', { name: 'Cancel' }).click()
  await expect(guide.getByRole('note')).toContainText('Welcome back, @ada.')
  await guide.getByRole('button', { name: 'Skip to agent setup' }).click()
  await expect(guide.getByRole('heading', { name: /Bring your/ })).toBeFocused()
})

test('claiming a handle in the guide does not turn a new owner into a returning one', async ({ page }) => {
  await stubIdentity(page, linked(null))
  const { guide } = await openGuide(page)
  await expect(guide.getByRole('note')).toHaveCount(0)
  await guide.getByLabel('Choose your handle').fill('ada')
  await guide.getByRole('button', { name: 'Claim handle' }).click()
  await expect(guide.getByText('@ada')).toBeVisible()
  await expect(guide.getByRole('note')).toHaveCount(0)
  await expect(guide.getByRole('button', { name: 'Skip to agent setup' })).toHaveCount(0)
  // Leaving the screen and coming back keeps it that way.
  await guide.getByRole('button', { name: 'Continue' }).click()
  await guide.getByRole('button', { name: 'Back' }).click()
  await expect(guide.getByRole('note')).toHaveCount(0)
})

test('a short desktop window keeps the chat preview reachable instead of squashing it', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 640 })
  const { guide } = await openGuide(page)
  await goToScreen(guide, 'Meet your agent')
  const preview = guide.locator('.wt-chatmock')
  // Layout pixels: the shell zooms the page on desktop, which would skew an on-screen measurement.
  expect(await preview.evaluate(element => element.offsetHeight)).toBeGreaterThanOrEqual(334)
  const composer = guide.locator('.wt-chatmock__composer')
  await composer.scrollIntoViewIfNeeded()
  await expect(composer).toBeInViewport()
  const [inner, outer] = [await composer.boundingBox(), await preview.boundingBox()]
  expect(inner.y + inner.height).toBeLessThanOrEqual(outer.y + outer.height + 1)
})
