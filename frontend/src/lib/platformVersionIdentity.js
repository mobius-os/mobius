function shortSha(value) {
  const sha = typeof value === 'string' ? value.trim() : ''
  return sha && sha !== 'unknown' ? sha.slice(0, 7) : null
}

export function platformVersionIdentity(platform, version) {
  const syncedSha = shortSha(platform?.contained_upstream_sha)
    || shortSha(platform?.recorded_upstream_sha)
  const servedSha = shortSha(version?.served_sha) || shortSha(version?.sha)
  return {
    primarySha: syncedSha || servedSha,
    synced: !!syncedSha,
    localSha: syncedSha && servedSha && syncedSha !== servedSha ? servedSha : null,
  }
}
