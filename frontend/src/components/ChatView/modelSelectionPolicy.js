/* Owns the model choice the interactive composer presents and sends. */

function configuredModel(value) {
  return typeof value === 'string' && value.trim() ? value : null
}

export function resolvedChatSettings(chatInfo) {
  const explicit = chatInfo?.agent_settings_json
  const effective = chatInfo?.effective
  return {
    ...(explicit && typeof explicit === 'object' ? explicit : {}),
    ...(effective && typeof effective === 'object' ? effective : {}),
    // The explicit per-chat choice is durable truth. A retained browser cache
    // can briefly lack the derived `effective` projection after an upgrade;
    // never make that look like the owner lost their saved model.
    model: configuredModel(effective?.model) ?? configuredModel(explicit?.model),
  }
}

export function selectedChatModel(chatInfo) {
  return resolvedChatSettings(chatInfo).model
}

export function needsModelSelection({ showPicker, chatInfo }) {
  if (!showPicker) return false
  // A cold activation has not loaded the chat's runtime settings yet. Let the
  // send reach the authoritative backend: an already-selected chat proceeds,
  // while a genuinely unselected chat returns MODEL_SELECTION_REQUIRED and
  // opens the same picker through the normal failure path. Treating unknown as
  // missing made a fast first send impossible even when the persisted chat had
  // an explicit model.
  if (chatInfo == null) return false
  return !selectedChatModel(chatInfo)
}
