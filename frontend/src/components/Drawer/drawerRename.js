/** The server's name cap for chat titles and app names. */
export function drawerNameMaxLength() {
  return 500
}

/**
 * Save one drawer rename and report a failure instead of letting the row
 * silently fall back to its old name. `save` may resolve to a fetch Response
 * (a non-ok status is a failure) or to any other value, or reject.
 */
export async function saveDrawerRename(save, notify) {
  let saved = false
  try {
    const result = await save()
    saved = result?.ok !== false
  } catch {}
  if (!saved) notify?.('Couldn’t rename that item.', { variant: 'error' })
  return saved
}
