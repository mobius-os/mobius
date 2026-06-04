export function appVersionKey(updatedAt) {
  if (updatedAt == null) return '0'
  const value = String(updatedAt).trim()
  return value || '0'
}
