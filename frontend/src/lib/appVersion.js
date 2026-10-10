// An app frame reloads only when this key changes. The server derives
// frame_version from what the frame executes (bundle, runtime declarations,
// storage generation), so settings writes that advance updated_at keep the
// running frame and its place. During deployment the old backend may still
// serve rows without frame_version; keep its updated_at reload key until restart.
// '0' is the missing-row sentinel.
export function appFrameVersion(app) {
  const value = typeof app?.frame_version === 'string' ? app.frame_version.trim() : ''
  return value || app?.updated_at || '0'
}

// The frame ?v is `<appFrameVersion>-<frameRev>` where frameRev is the shared
// app-frame.html content hash (theme.frame_content_rev), exactly 16 lowercase
// hex. The MODULE cache key must drop frameRev so a frame-only redeploy does
// not bust every app's module cache (kept in sync with the inline copy in
// public/app-frame.html loadModule). Anchored + exactly-16 so a real
// frame_version (unhyphenated hex) or hyphenated semver prerelease is
// never shortened.
export function moduleVersionKey(frameV) {
  return String(frameV ?? '0').replace(/-[0-9a-f]{16}$/, '')
}
