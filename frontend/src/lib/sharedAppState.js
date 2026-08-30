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
