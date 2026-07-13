export function createProviderSwitchId() {
  return globalThis.crypto?.randomUUID?.()
    || `switch-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

export function providerSwitchPayload({
  provider,
  model,
  effort,
  effortByProvider,
  switchId,
}) {
  return {
    provider,
    switch_id: switchId,
    agent_settings_json: {
      model,
      effort,
      effort_by_provider: effortByProvider,
    },
  }
}

export function restorableProviderSwitch(saved, chatId, sourceProvider) {
  if (
    !saved
    || saved.chatId !== chatId
    || saved.sourceProvider !== sourceProvider
  ) {
    return null
  }
  return saved
}

export async function providerSwitchResponseData(response) {
  try {
    const data = await response.json()
    return data && typeof data === 'object' ? data : null
  } catch {
    // A 2xx status with a truncated/unreadable body is ambiguous: the server
    // may already have committed. The caller must retain the switch_id and
    // retry rather than pretending it received authoritative provider state.
    return null
  }
}
