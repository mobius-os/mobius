/**
 * Behavior contracts for the platform update result dialog.
 *
 * Every platform endpoint is intercepted, so these tests exercise the real
 * Settings UI without fetching, applying, resolving, or restarting anything.
 *
 * Run: scripts/playwright-local.sh --allow-local-e2e tests/platform-update-modal.spec.mjs
 */
import { test, expect } from '@playwright/test'

const BASE = process.env.MOBIUS_URL || 'http://localhost:8001'

test.use({ serviceWorkers: 'block' })

function platformStatus(state = 'available') {
  return {
    state,
    available: state === 'available' || state === 'rolled_back',
    needs_restart: state === 'restart_needed',
    current_build_sha: '1111111111111111111111111111111111111111',
    recorded_upstream_sha: '1111111111111111111111111111111111111111',
    contained_upstream_sha: '1111111111111111111111111111111111111111',
    seed_required: false,
    conflict_paths: state === 'conflict' ? ['frontend/src/example.js'] : [],
    conflict_chat_id: null,
  }
}

const preview = {
  state: 'available',
  available: true,
  current_sha: '1111111111111111111111111111111111111111',
  target_sha: '2222222222222222222222222222222222222222',
  plan_id: 'a'.repeat(64),
  total_commits: 1,
  commits_truncated: false,
  commits: [{ sha: '22222222', subject: 'Incoming platform change' }],
  files: [],
  diff: null,
  diff_truncated: false,
  conflict_paths: [],
}

function deferred() {
  let resolve
  const promise = new Promise(done => { resolve = done })
  return { promise, resolve }
}

async function mockPlatform(page, stateRef) {
  await page.route('**/api/platform/status', route => {
    if (stateRef.failStatus) {
      return route.fulfill({
        status: 503,
        contentType: 'application/json',
        body: JSON.stringify({ detail: 'Status temporarily unavailable.' }),
      })
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(platformStatus(stateRef.current)),
    })
  })
  await page.route('**/api/platform/update-preview', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(preview),
  }))
  await page.route('**/api/platform/update-progress', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify({
      plan_id: preview.plan_id,
      target_sha: preview.target_sha,
      phase: 'building',
      active: true,
      error: null,
      updated_at: Date.now() / 1000,
    }),
  }))
}

async function openUpdateReview(page) {
  await page.setViewportSize({ width: 900, height: 800 })
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
  await expect(page.locator('.drawer.drawer--open')).toBeVisible()
  await page.getByRole('button', { name: 'Settings', exact: true }).click()
  await expect(page.locator('.settings')).toBeVisible()
  await expect(page.getByText('New update available', { exact: true })).toBeVisible()

  await page.getByRole('button', { name: 'Review update', exact: true }).click()
  const dialog = page.getByRole('dialog', { name: 'Review update' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Apply update' })).toBeEnabled()
  return dialog
}

test('a clean apply closes the review and exposes the restart step', async ({ page }) => {
  const state = { current: 'available' }
  await mockPlatform(page, state)
  let appliedPlan = null
  await page.route('**/api/platform/apply', route => {
    appliedPlan = route.request().postDataJSON()
    state.current = 'restart_needed'
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        state: 'restart_needed',
        needs_restart: true,
        upstream_commit: preview.target_sha,
        merge_commit: '3333333333333333333333333333333333333333',
        conflict_paths: [],
        chat_id: null,
      }),
    })
  })

  const dialog = await openUpdateReview(page)
  await dialog.getByRole('button', { name: 'Apply update' }).click()

  await expect(dialog).toHaveCount(0)
  expect(appliedPlan).toEqual({
    plan_id: preview.plan_id,
    current_sha: preview.current_sha,
    target_sha: preview.target_sha,
  })
  const restart = page.getByRole('button', { name: 'Restart to finish' })
  await expect(restart).toBeVisible()
  await expect(restart).toBeFocused()
})

test('a blocked apply stays open, focuses its result, and shows resolver failures', async ({ page }) => {
  const state = { current: 'available' }
  await mockPlatform(page, state)
  await page.route('**/api/platform/apply', route => {
    state.current = 'conflict'
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        state: 'conflict',
        needs_restart: false,
        upstream_commit: preview.target_sha,
        merge_commit: null,
        conflict_paths: ['frontend/src/example.js'],
        chat_id: null,
      }),
    })
  })

  const resolver = deferred()
  await page.route('**/api/platform/conflict-resolver-chat', async route => {
    await resolver.promise
    return route.fulfill({
      status: 409,
      contentType: 'application/json',
      body: JSON.stringify({ detail: 'The recorded conflict is no longer resolvable.' }),
    })
  })

  const review = await openUpdateReview(page)
  await review.getByRole('button', { name: 'Apply update' }).click()

  const blocked = page.getByRole('dialog', { name: 'Update not applied' })
  await expect(blocked).toBeVisible()
  await expect(blocked.getByText('Your current version is still running.')).toBeVisible()

  const resolveButton = blocked.getByRole('button', { name: 'Resolve in chat' })
  await expect(resolveButton).toBeFocused()
  await page.keyboard.press('Shift+Tab')
  await expect(blocked.getByRole('button', { name: 'Not now' })).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(resolveButton).toBeFocused()

  await resolveButton.click()
  await expect(blocked.getByRole('button', { name: 'Opening…' })).toBeDisabled()
  await page.keyboard.press('Shift+Tab')
  await expect(blocked).toBeFocused()
  await page.keyboard.press('Escape')
  await expect(blocked).toBeVisible()

  resolver.resolve()
  const error = blocked.locator('.urm__error')
  await expect(error.getByRole('alert')).toContainText(
    'Could not open chat: The recorded conflict is no longer resolvable.',
  )
  await expect(blocked.getByRole('alert')).toHaveCount(1)
  await expect(blocked.getByRole('button', { name: 'Resolve in chat' })).toBeEnabled()

  await page.locator('.urm__overlay').click({ position: { x: 2, y: 2 } })
  await expect(blocked).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Resolve in chat' })).toBeFocused()
})

test('a rolled-back apply stays open with an explicit repair result', async ({ page }) => {
  const state = { current: 'available' }
  await mockPlatform(page, state)
  await page.route('**/api/platform/apply', route => {
    state.current = 'rolled_back'
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        state: 'rolled_back',
        needs_restart: false,
        upstream_commit: preview.target_sha,
        merge_commit: null,
        conflict_paths: [],
        chat_id: null,
      }),
    })
  })

  const review = await openUpdateReview(page)
  await review.getByRole('button', { name: 'Apply update' }).click()

  const result = page.getByRole('dialog', { name: 'Update rolled back' })
  await expect(result).toBeVisible()
  await expect(result.getByText('Your previous working version was restored.')).toBeVisible()
  const done = result.getByRole('button', { name: 'Done', exact: true })
  await expect(done).toBeFocused()
  await done.click()
  await expect(result).toHaveCount(0)
  await expect(page.getByText('Update needs repair', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Review update', exact: true })).toBeFocused()
})

test('a clean apply remains truthful when every follow-up status read fails', async ({ page }) => {
  const state = { current: 'available', failStatus: false }
  await mockPlatform(page, state)
  await page.route('**/api/platform/apply', route => {
    state.failStatus = true
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        state: 'restart_needed',
        needs_restart: true,
        upstream_commit: preview.target_sha,
        merge_commit: '3333333333333333333333333333333333333333',
        conflict_paths: [],
        chat_id: null,
      }),
    })
  })

  const review = await openUpdateReview(page)
  await review.getByRole('button', { name: 'Apply update' }).click()

  await expect(review).toHaveCount(0)
  await expect(
    page.locator('.settings__update').getByText('Restart to finish', { exact: true }),
  ).toBeVisible()
  await expect(page.getByRole('button', { name: 'Restart to finish' })).toBeFocused()
})

test('a conflict result closes to truthful repair state when status reads fail', async ({ page }) => {
  const state = { current: 'available', failStatus: false }
  await mockPlatform(page, state)
  await page.route('**/api/platform/apply', route => {
    state.failStatus = true
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        state: 'conflict',
        needs_restart: false,
        upstream_commit: preview.target_sha,
        merge_commit: null,
        conflict_paths: ['frontend/src/example.js'],
        chat_id: null,
      }),
    })
  })

  const review = await openUpdateReview(page)
  await review.getByRole('button', { name: 'Apply update' }).click()

  const result = page.getByRole('dialog', { name: 'Update not applied' })
  await expect(result).toBeVisible()
  await result.getByRole('button', { name: 'Not now' }).click()
  await expect(result).toHaveCount(0)
  await expect(page.getByText('Update blocked', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Resolve in chat' })).toBeFocused()
})

test('a rollback result closes to truthful retry state when status reads fail', async ({ page }) => {
  const state = { current: 'available', failStatus: false }
  await mockPlatform(page, state)
  await page.route('**/api/platform/apply', route => {
    state.failStatus = true
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        state: 'rolled_back',
        needs_restart: false,
        upstream_commit: preview.target_sha,
        merge_commit: null,
        conflict_paths: [],
        chat_id: null,
      }),
    })
  })

  const review = await openUpdateReview(page)
  await review.getByRole('button', { name: 'Apply update' }).click()

  const result = page.getByRole('dialog', { name: 'Update rolled back' })
  await expect(result).toBeVisible()
  await result.getByRole('button', { name: 'Done', exact: true }).click()
  await expect(result).toHaveCount(0)
  await expect(page.getByText('Update needs repair', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Review update', exact: true })).toBeFocused()
})

test('malformed, missing, and unknown successful states fail open and can retry', async ({ page }) => {
  const state = { current: 'available' }
  await mockPlatform(page, state)
  let attempts = 0
  await page.route('**/api/platform/apply', route => {
    attempts += 1
    if (attempts === 1) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{' })
    }
    if (attempts === 2) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: '{}' })
    }
    if (attempts === 3) {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ state: 'future_state' }),
      })
    }
    state.current = 'restart_needed'
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({ state: 'restart_needed', needs_restart: true }),
    })
  })

  const review = await openUpdateReview(page)
  const apply = review.getByRole('button', { name: 'Apply update' })
  for (let attempt = 1; attempt <= 3; attempt += 1) {
    await apply.click()
    await expect(review).toBeVisible()
    await expect(review.locator('.urm__error').getByRole('alert')).toContainText(
      'The update returned an unexpected result.',
    )
    await expect(review.getByRole('alert')).toHaveCount(1)
    await expect(apply).toBeEnabled()
  }

  await apply.click()
  await expect(review).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Restart to finish' })).toBeFocused()
})

test('Escape and dismissal stay gated while Apply is pending', async ({ page }) => {
  const state = { current: 'available' }
  await mockPlatform(page, state)
  const apply = deferred()
  await page.route('**/api/platform/apply', async route => {
    await apply.promise
    state.current = 'restart_needed'
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify({
        state: 'restart_needed',
        needs_restart: true,
        upstream_commit: preview.target_sha,
        merge_commit: '3333333333333333333333333333333333333333',
        conflict_paths: [],
        chat_id: null,
      }),
    })
  })

  const dialog = await openUpdateReview(page)
  await dialog.getByRole('button', { name: 'Apply update' }).click()
  await expect(dialog.getByRole('button', { name: 'Applying…' })).toBeDisabled()
  await expect(dialog.getByText('Building the frontend…')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Not now' })).toBeDisabled()
  await expect(dialog.getByRole('button', { name: 'Close' })).toBeDisabled()

  await page.keyboard.press('Escape')
  await expect(dialog).toBeVisible()

  apply.resolve()
  await expect(dialog).toHaveCount(0)
})
