export function normalizeSharedAppSnapshot(current, next) {
  const currentCursor = Number(current?.cursor || 0)
  const nextCursor = Number(next?.cursor || 0)
  if (nextCursor < currentCursor) return null
  return {
    cursor: nextCursor,
    values: next?.values || {},
    versions: next?.versions || {},
  }
}


export function applySharedAppMutation(current, path, value, version, deleted = false) {
  const values = { ...(current?.values || {}) }
  const versions = { ...(current?.versions || {}) }
  if (deleted) {
    delete values[path]
    delete versions[path]
  } else {
    values[path] = value
    versions[path] = version
  }
  return {
    // A mutation response identifies this write, but does not contain state
    // for collaborator writes that may precede it. Retaining the last fully
    // observed cursor makes the next change poll recover the entire interval.
    cursor: Number(current?.cursor || 0),
    values,
    versions,
  }
}
