const RECOVERY_TYPES = Object.freeze({
  recover_chat: 'chat',
  recover_app: 'app',
  recover_project: 'project',
})

const RESOURCE_ID = /^[A-Za-z0-9._:-]{1,128}$/

// Notification actions are untrusted app/agent-authored data. Return only the
// exact recovery shape the owner shell knows how to execute; everything else
// remains inert presentation data.
export function parseNotificationRecoveryAction(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const resourceType = RECOVERY_TYPES[value.action]
  if (!resourceType || value.resource_type !== resourceType) return null
  if (typeof value.resource_id !== 'string' || !RESOURCE_ID.test(value.resource_id)) return null
  if (value.target != null) return null
  if (typeof value.title !== 'string' || !value.title.trim()) return null
  if (
    value.completed_at != null
    && (
      typeof value.completed_at !== 'string'
      || Number.isNaN(Date.parse(value.completed_at))
    )
  ) return null
  return {
    action: value.action,
    title: value.title,
    resourceType,
    resourceId: value.resource_id,
    completedAt: value.completed_at || null,
  }
}

export function notificationRecoveryAction(notification) {
  if (!Array.isArray(notification?.actions)) return null
  for (const value of notification.actions) {
    const action = parseNotificationRecoveryAction(value)
    if (action) return action
  }
  return null
}
