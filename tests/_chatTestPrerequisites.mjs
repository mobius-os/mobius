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
