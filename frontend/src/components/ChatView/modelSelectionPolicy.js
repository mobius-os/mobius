/* Decides when the interactive composer must ask for an explicit model. */

export function needsModelSelection({ showPicker, chatInfo }) {
  if (!showPicker) return false
  // A cold activation has not loaded the chat's runtime settings yet. Let the
  // send reach the authoritative backend: an already-selected chat proceeds,
  // while a genuinely unselected chat returns MODEL_SELECTION_REQUIRED and
  // opens the same picker through the normal failure path. Treating unknown as
  // missing made a fast first send impossible even when the persisted chat had
  // an explicit model.
  if (chatInfo == null) return false
  return !chatInfo?.effective?.model
}
