export function appVersionKey(updatedAt) {
  if (updatedAt == null) return '0'
  const value = String(updatedAt).trim()
  return value || '0'
}

// The frame ?v is `<appVersionKey>-<frameRev>` where frameRev is the shared
// app-frame.html content hash (theme.frame_content_rev), exactly 16 lowercase
// hex. The MODULE cache key must drop frameRev so a frame-only redeploy does
// not bust every app's module cache (kept in sync with the inline copy in
// public/app-frame.html loadModule). Anchored + exactly-16 so a real
// updated_at (digit-only microseconds) or hyphenated semver prerelease is
// never shortened.
export function moduleVersionKey(frameV) {
  return String(frameV ?? '0').replace(/-[0-9a-f]{16}$/, '')
}
