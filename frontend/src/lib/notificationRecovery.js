// Parse and present tombstone-bound recovery receipts without navigation side effects.
const RECOVERY_TYPES = Object.freeze({
  recover_chat: 'chat',
  recover_app: 'app',
  recover_project: 'project',
})

const RESOURCE_ID = /^[A-Za-z0-9._:-]{1,128}$/
const RESOURCE_GENERATION = /^[a-f0-9]{64}$/

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
  if (
    typeof value.resource_generation !== 'string'
    || !RESOURCE_GENERATION.test(value.resource_generation)
    || typeof value.deleted_at !== 'string'
    || typeof value.expires_at !== 'string'
    || !Number.isFinite(Date.parse(value.deleted_at))
    || !Number.isFinite(Date.parse(value.expires_at))
    || Date.parse(value.expires_at) <= Date.parse(value.deleted_at)
  ) return null
  return {
    action: value.action,
    title: value.title,
    resourceType,
    resourceId: value.resource_id,
    completedAt: value.completed_at || null,
    expiresAt: value.expires_at,
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

export function recoveryUnavailableLabel(action, now = Date.now()) {
  if (action.completedAt) return 'Restored'
  return now >= Date.parse(action.expiresAt) ? 'Recovery window expired' : null
}

export function recoveryFailure(error) {
  if (error?.status === 410) return { terminal: true, message: 'Recovery window expired' }
  if (error?.code === 'recovery_superseded') {
    return { terminal: true, message: 'Earlier deletion — use the latest Undo' }
  }
  if (error?.code === 'recovery_already_restored') {
    return { terminal: true, message: 'Already restored' }
  }
  if (error?.status === 404) return { terminal: true, message: 'Recovery no longer available' }
  return { terminal: false, message: error?.message || 'Couldn’t restore this item. Try again.' }
}

export function completeNotificationRecovery(history, notificationId, completedAt) {
  if (!history) return history
  return {
    ...history,
    pages: history.pages.map(page => page.map(row => (
      row.id !== notificationId ? row : {
        ...row,
        actions: (row.actions || []).map(action => (
          parseNotificationRecoveryAction(action)
            ? { ...action, completed_at: completedAt }
            : action
        )),
      }
    ))),
  }
}
