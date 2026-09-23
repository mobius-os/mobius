export const TEST_CHAT_MODEL = process.env.MOBIUS_TEST_MODEL || 'claude-sonnet-4-6'

/** Mirror both settings views returned by a hydrated chat-detail response. */
export function testChatAgentSettings() {
  return {
    agent_settings_json: { model: TEST_CHAT_MODEL },
    effective_agent_settings: {
      model: TEST_CHAT_MODEL,
      effort: 'medium',
    },
  }
}

/**
 * Return a quota-positive snapshot for mocked provider traffic.
 *
 * Browser fixtures must not fall through to the disposable backend's real
 * provider probe: its disconnected state makes the composer appear unusable
 * even when the suite has explicitly configured that provider. Keep this
 * snapshot small and typed around the allowance kind the UI consumes.
 */
export function testProviderUsageSnapshot() {
  return {
    state: 'ready',
    plan_label: 'Test plan',
    windows: [{
      id: 'weekly',
      kind: 'weekly',
      label: 'Weekly',
      used_percent: 10,
      resets_at: '2030-01-08T00:00:00+00:00',
    }],
    credit_balance: null,
    extra_usage: { enabled: false, available: false, used_percent: null },
  }
}

/** Install the deterministic provider-usage boundary used by chat fixtures. */
export async function installMockProviderUsage(page) {
  await page.route('**/api/settings/provider-usage/*', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    json: testProviderUsageSnapshot(),
  }))
}

/** Simulate the provider boundary paired with the suite's mocked agent traffic. */
export async function installMockAgentProvider(page) {
  await page.route('**/api/auth/providers/status', route => route.fulfill({
    status: 200,
    contentType: 'application/json',
    json: {
      claude: {
        name: 'Claude Code',
        configured: true,
        authenticated: true,
        error: null,
      },
      codex: {
        name: 'Codex',
        configured: false,
        authenticated: false,
        error: 'Not connected in this test fixture.',
      },
    },
  }))
  await installMockProviderUsage(page)
}

/** Persist the explicit first-send choice required by production chat policy. */
export async function persistTestChatModel(page, { base, chatId, token }) {
  return page.request.patch(`${base}/api/chats/${chatId}`, {
    headers: { Authorization: `Bearer ${token}` },
    data: {
      agent_settings_json: testChatAgentSettings().agent_settings_json,
    },
    failOnStatusCode: false,
  })
}
