function appTokenClaims(token) {
  try {
    const encoded = String(token || '').split('.')[1]
    if (!encoded) return null
    const normalized = encoded.replace(/-/g, '+').replace(/_/g, '/')
    return JSON.parse(atob(normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=')))
  } catch (e) {
    return null
  }
}

export function tokenMatchesRuntime(token, appId, appInstanceId) {
  const claims = appTokenClaims(token)
  if (!claims || claims.scope !== 'app' || String(claims.app_id) !== String(appId)) {
    return false
  }
  const tokenInstance = typeof claims.app_nonce === 'string' && claims.app_nonce
    ? claims.app_nonce
    : null
  return appInstanceId ? tokenInstance === appInstanceId : tokenInstance === null
}
