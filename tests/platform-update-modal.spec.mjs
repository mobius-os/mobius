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

function platformStatus(state = 'available', overrides = {}) {
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
    ...overrides,
  }
}

const preview = {
  state: 'available',
  available: true,
  actionable: true,
  operation: 'update',
  activation: { level: 'live', deployment: 'self_hosted', reasons: [], guidance: [] },
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
  // Safety net registered before specific handlers (Playwright routes are LIFO).
  // An unmocked updater request must fail in the test, never reach the instance.
  stateRef.unexpectedMutations = []
  await page.route('**/api/platform/**', route => {
    if (route.request().method() !== 'GET') stateRef.unexpectedMutations.push(route.request().url())
    return route.fulfill({ status: 501, contentType: 'application/json',
      body: JSON.stringify({ detail: 'Unmocked platform request in test.' }) })
  })
  await page.route(/\/api\/admin\/(restart|rebuild)(?:[/?]|$)/, route => {
    if (route.request().method() !== 'GET') stateRef.unexpectedMutations.push(route.request().url())
    return route.fulfill({ status: route.request().method() === 'GET' ? 200 : 501,
      contentType: 'application/json', body: JSON.stringify(stateRef.rebuild || {
        supported: true, deployment: 'self_hosted', state: 'idle',
      }) })
  })
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
      body: JSON.stringify(platformStatus(stateRef.current, stateRef.overrides)),
    })
  })
  await page.route('**/api/platform/update-preview*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    body: JSON.stringify(stateRef.preview || preview),
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

async function openSettings(page, width = 900) {
  await page.setViewportSize({ width, height: 800 })
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
  return page.locator('.platform-updates')
}

async function openUpdateReview(page) {
  await openSettings(page)
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

test('a staged update can check for and review another release before one restart', async ({ page }) => {
  const state = {
    current: 'restart_needed',
    overrides: { available: false, needs_restart: true },
  }
  await mockPlatform(page, state)
  await page.route('**/api/platform/check', route => {
    state.overrides = { available: true, needs_restart: true }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(platformStatus('restart_needed', state.overrides)),
    })
  })
  await page.route('**/api/platform/apply', route => {
    state.overrides = { available: false, needs_restart: true }
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
  await page.getByRole('button', { name: 'Settings', exact: true }).click()

  await expect(page.getByText('Ready to restart', { exact: true })).toBeVisible()
  const check = page.getByRole('button', { name: 'Check for more' })
  await expect(check).toBeVisible()
  await expect(page.getByRole('button', { name: 'Restart to finish' })).toBeVisible()
  await check.click()

  await expect(page.getByText('More updates available', { exact: true })).toBeVisible()
  const review = page.getByRole('button', { name: 'Review update' })
  await expect(review).toBeVisible()
  await expect(review).toBeFocused()
  await expect(page.getByRole('button', { name: 'Restart to finish' })).toBeVisible()
  await review.click()

  const dialog = page.getByRole('dialog', { name: 'Review update' })
  await expect(dialog).toBeVisible()
  await dialog.getByRole('button', { name: 'Apply update' }).click()

  await expect(dialog).toHaveCount(0)
  await expect(page.getByText('Ready to restart', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Check for more' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Restart to finish' })).toBeFocused()
})

test('staged-update actions stack without overflow in a narrow settings pane', async ({ page }) => {
  const state = {
    current: 'restart_needed',
    overrides: { available: true, needs_restart: true },
  }
  await mockPlatform(page, state)

  await page.setViewportSize({ width: 360, height: 780 })
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

  const actions = page.locator('.platform-updates__actions').first()
  await expect(actions).toBeVisible()
  const box = await actions.boundingBox()
  const viewport = page.viewportSize()
  expect(box).not.toBeNull()
  expect(box.x).toBeGreaterThanOrEqual(0)
  expect(box.x + box.width).toBeLessThanOrEqual(viewport.width)
  await expect(page.getByRole('button', { name: 'Review update' })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Restart to finish' })).toBeVisible()
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
    'The recorded conflict is no longer resolvable.',
  )
  await expect(blocked.getByRole('alert')).toHaveCount(1)
  await expect(blocked.getByRole('button', { name: 'Resolve in chat' })).toBeEnabled()

  await page.locator('.urm__overlay').click({ position: { x: 2, y: 2 } })
  await expect(blocked).toHaveCount(0)
  await expect(page.getByRole('button', { name: 'Resolve in chat' })).toBeFocused()
})

test('a rolled-back apply stays open with an explicit repair action', async ({ page }) => {
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
  await expect(result.getByText('Your previous source was restored.')).toBeVisible()
  await expect(result.getByRole('button', { name: 'Ask Möbius' })).toBeFocused()
  await result.getByRole('button', { name: 'Not now' }).click()
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
    page.locator('.platform-updates').getByText('Ready to restart', { exact: true }),
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

test('a rollback result keeps an explicit repair action when status reads fail', async ({ page }) => {
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
  await expect(result.getByRole('button', { name: 'Ask Möbius' })).toBeFocused()
  await result.getByRole('button', { name: 'Not now' }).click()
  await expect(result).toHaveCount(0)
  await expect(page.getByText('Update needs repair', { exact: true })).toBeVisible()
  await expect(page.getByRole('button', { name: 'Review update', exact: true })).toBeFocused()
})

for (const [label, body] of [
  ['malformed', '{'],
  ['missing state', '{}'],
  ['unknown state', JSON.stringify({ state: 'future_state' })],
]) {
  test(`${label} successful apply results fail open and route to repair`, async ({ page }) => {
    const state = { current: 'available' }
    await mockPlatform(page, state)
    await page.route('**/api/platform/apply', route => route.fulfill({
      status: 200,
      contentType: 'application/json',
      body,
    }))

    const review = await openUpdateReview(page)
    await review.getByRole('button', { name: 'Apply update' }).click()
    await expect(review).toBeVisible()
    await expect(review.locator('.urm__error').getByRole('alert')).toContainText(
      'The update returned an unexpected result.',
    )
    await expect(review.getByRole('alert')).toHaveCount(1)
    await expect(review.getByRole('button', { name: 'Ask Möbius' })).toBeFocused()
    await expect(review.getByRole('button', { name: 'Apply update' })).toHaveCount(0)
  })
}

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
  await expect(dialog.getByRole('button', { name: 'Updating…' })).toBeDisabled()
  await expect(dialog.getByText('Preparing dependencies and the interface…')).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Not now' })).toBeDisabled()
  await expect(dialog.getByRole('button', { name: 'Close' })).toBeDisabled()

  await page.keyboard.press('Escape')
  await expect(dialog).toBeVisible()

  apply.resolve()
  await expect(dialog).toHaveCount(0)
})

const imageActivation = {
  level: 'image_rebuild', deployment: 'self_hosted',
  reasons: [{ code: 'baked_runtime', summary: 'Container runtime changed.', paths: ['backend/runtime/example.py'] }],
  guidance: [],
}

function finishPreview(overrides = {}) {
  return { ...preview, available: false, actionable: true, operation: 'finish',
    state: 'activation_needed', target_sha: preview.current_sha,
    activation: imageActivation, commits: [], total_commits: 0, ...overrides }
}

test('an installed image update can finish without a newer source release', async ({ page }) => {
  const state = { current: 'activation_needed',
    overrides: { available: false, activation: imageActivation }, preview: finishPreview() }
  await mockPlatform(page, state)
  const updates = await openSettings(page)
  await expect(updates.getByRole('button', { name: 'Finish update', exact: true })).toBeVisible()
  await expect(updates.getByRole('button', { name: 'Review update', exact: true })).toHaveCount(0)
  const request = page.waitForRequest('**/api/platform/update-preview?intent=finish')
  await updates.getByRole('button', { name: 'Finish update', exact: true }).click()
  await request
  const dialog = page.getByRole('dialog', { name: 'Finish update' })
  await expect(dialog).toBeVisible()
  await expect(dialog.getByRole('button', { name: 'Update container now' })).toBeEnabled()
  await expect(dialog.getByText('Make the installed update active')).toBeVisible()
  await expect(dialog.getByText('There’s nothing to apply. This update is already complete.')).toHaveCount(0)
  expect(state.unexpectedMutations).toEqual([])
})

test('finish submits the exact reviewed plan, not the newer available release', async ({ page }) => {
  const installed = finishPreview({ activation: { ...imageActivation, deployment: 'railway' },
    image_digest: `sha256:${'b'.repeat(64)}` })
  const state = { current: 'activation_needed',
    overrides: { available: true, activation: imageActivation }, preview: installed }
  await mockPlatform(page, state)
  await page.route('**/api/health', route => route.fulfill({ status: 200,
    contentType: 'application/json', body: JSON.stringify({ status: 'ok', boot_id: 'before' }) }))
  let submitted = null
  await page.route('**/api/platform/rebuild', route => {
    submitted = route.request().postDataJSON()
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({
      supported: true, deployment: 'railway', state: 'no_change', expected_sha: installed.target_sha,
    }) })
  })
  const updates = await openSettings(page)
  await expect(updates.getByRole('button', { name: 'Review update', exact: true })).toBeVisible()
  const request = page.waitForRequest('**/api/platform/update-preview?intent=finish')
  await updates.getByRole('button', { name: 'Finish installed update' }).click()
  await request
  const dialog = page.getByRole('dialog', { name: 'Finish update' })
  await dialog.getByRole('button', { name: 'Update container now' }).click()
  await expect(dialog).toHaveCount(0)
  expect(submitted).toEqual({ plan_id: installed.plan_id, current_sha: installed.current_sha,
    target_sha: installed.target_sha, image_digest: installed.image_digest })
  expect(state.unexpectedMutations).toEqual([])
})

test('a failed container result survives reopening Settings without an unsolicited alert', async ({ page }) => {
  const state = { current: 'activation_needed', overrides: { available: false, activation: imageActivation },
    rebuild: { supported: true, state: 'failed', expected_sha: preview.current_sha,
      updated_at: '2026-09-05T11:00:00Z', error: 'The reviewed image could not start.' } }
  await mockPlatform(page, state)
  let updates = await openSettings(page)
  const error = 'The reviewed image could not start.'
  await expect(updates.getByText(error)).not.toBeVisible()
  await updates.getByText('Details and maintenance', { exact: true }).click()
  await expect(updates.getByRole('heading', { name: 'Last container update' })).toBeVisible()
  await expect(updates.getByText(error)).toBeVisible()
  // A full reload exercises a fresh component with no in-memory request owner.
  updates = await openSettings(page)
  await updates.getByText('Details and maintenance', { exact: true }).click()
  await expect(updates.getByText(error)).toBeVisible()
  await expect(updates.getByRole('button', { name: 'Finish update', exact: true })).toBeEnabled()
  expect(state.unexpectedMutations).toEqual([])
})

for (const level of ['server_restart', 'dependency_sync']) {
  test(`${level} completion asks before restarting and cancellation performs no mutation`, async ({ page }) => {
    const state = { current: 'restart_needed', overrides: { available: false, needs_restart: true,
      activation: { level, deployment: 'self_hosted', reasons: [], guidance: [] } } }
    await mockPlatform(page, state)
    const updates = await openSettings(page)
    await updates.getByRole('button', { name: 'Restart to finish' }).click()
    const confirmation = updates.getByRole('group', { name: 'Confirm restart' })
    await expect(confirmation).toBeVisible()
    await expect(confirmation.getByRole('button', { name: 'Restart now' })).toBeEnabled()
    await expect(confirmation).toContainText('briefly interrupts active chats')
    expect(state.unexpectedMutations).toEqual([])
    await confirmation.getByRole('button', { name: 'Not now' }).click()
    await expect(confirmation).toHaveCount(0)
    await expect(updates.getByRole('button', { name: 'Restart to finish' })).toBeFocused()
    expect(state.unexpectedMutations).toEqual([])
  })
}

test('technical changes stay behind an optional disclosure in the review', async ({ page }) => {
  const state = { current: 'available', preview: { ...preview,
    files: [{ path: 'frontend/src/example.js', status: 'M', insertions: 1, deletions: 1 }],
    diff: 'diff --git a/frontend/src/example.js b/frontend/src/example.js\n--- a/frontend/src/example.js\n+++ b/frontend/src/example.js\n@@ -1 +1 @@\n-old\n+new\n' } }
  await mockPlatform(page, state)
  const dialog = await openUpdateReview(page)
  const technical = dialog.locator('details.urm__technical')
  await expect(technical).not.toHaveAttribute('open', '')
  await expect(dialog.getByRole('heading', { name: 'What to expect' })).toBeVisible()
  await expect(dialog.getByText('Incoming platform change')).not.toBeVisible()
  await technical.locator('summary').first().click()
  await expect(dialog.getByText('Incoming platform change')).toBeVisible()
  await expect(technical.getByTitle('frontend/src/example.js', { exact: true })).toBeVisible()
  expect(state.unexpectedMutations).toEqual([])
})
