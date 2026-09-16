export const E2E_SHARDS = [
  [
    'navigation.spec.mjs',
    'service-surface.spec.mjs',
    'send-rule.spec.mjs',
    'bootstrap.spec.mjs',
    'app-apply-lifecycle.spec.mjs',
    'chat-offline.spec.mjs',
    'composer-growth-cap.spec.mjs',
    'provider-availability.spec.mjs',
    'hidden-chat-settlement.spec.mjs',
    'slash-command-menu.spec.mjs',
  ],
  [
    'spacer.spec.mjs',
    'mode-transition.spec.mjs',
    'platform-update-modal.spec.mjs',
    'sw-pwa.spec.mjs',
    'cache.spec.mjs',
    'activity-lazy.spec.mjs',
    'embedded-chat-capability.spec.mjs',
    'attention-nudges.spec.mjs',
    'standalone-routing.spec.mjs',
    'setup-provider-status.spec.mjs',
    'project-source-list-layout.spec.mjs',
    'app-icon-visibility.spec.mjs',
  ],
  [
    'workspace-panes.spec.mjs',
    'shell-update-idle.spec.mjs',
    'quiet-answers.spec.mjs',
    'pin-clamp-settle.spec.mjs',
    'notifications.spec.mjs',
    'steer-queued.spec.mjs',
    'second-send-pin.spec.mjs',
    'composer-draft-attachments.spec.mjs',
    'settled-transcript-handoff.spec.mjs',
    'fresh-send-runtime-race.spec.mjs',
    'outbox.spec.mjs',
  ],
  [
    'stream-reconnect.spec.mjs',
    'frontend.spec.mjs',
    'chat-redesign.spec.mjs',
    'app-canvas.spec.mjs',
    'project-saved-collaboration.spec.mjs',
    'recovery-resume.spec.mjs',
    'storage-typed.spec.mjs',
    'image-gallery.spec.mjs',
    'question-follow.spec.mjs',
    'viewed-image-layout.spec.mjs',
    'embedded-chat-hostile.unauth.spec.mjs',
  ],
]

export const UNAUTHENTICATED_SPECS = new Set([
  'embedded-chat-hostile.unauth.spec.mjs',
])

export const TIMING_SPECS = new Set([
  'stream-reconnect.spec.mjs',
])

export function projectShard(projectName) {
  const match = /^shard-(\d)(?:-|$)/.exec(projectName)
  return match ? Number(match[1]) : null
}
