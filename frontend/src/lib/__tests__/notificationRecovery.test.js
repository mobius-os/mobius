import assert from 'node:assert/strict'
import test from 'node:test'

import {
  notificationRecoveryAction,
  parseNotificationRecoveryAction,
} from '../notificationRecovery.js'

test('recovery notifications accept only a matching inert resource action', () => {
  assert.deepEqual(parseNotificationRecoveryAction({
    action: 'recover_chat',
    title: 'Undo',
    resource_type: 'chat',
    resource_id: 'chat-123',
  }), {
    action: 'recover_chat',
    title: 'Undo',
    resourceType: 'chat',
    resourceId: 'chat-123',
    completedAt: null,
  })
  assert.equal(parseNotificationRecoveryAction({
    action: 'recover_chat', title: 'Undo', resource_type: 'app', resource_id: '7',
  }), null)
  assert.equal(parseNotificationRecoveryAction({
    action: 'recover_app', title: 'Undo', resource_type: 'app', resource_id: '../7',
  }), null)
  assert.equal(parseNotificationRecoveryAction({
    action: 'recover_project', title: 'Undo', resource_type: 'project',
    resource_id: 'p-1', target: '/shell/?chat=x',
  }), null)
  assert.equal(parseNotificationRecoveryAction({
    action: 'recover_project', title: 'Undo', resource_type: 'project',
    resource_id: 'p-1', completed_at: 'not-a-date',
  }), null)
})

test('completed recovery actions remain inspectable without another Undo', () => {
  const action = notificationRecoveryAction({
    actions: [{
      action: 'recover_app',
      title: 'Undo',
      resource_type: 'app',
      resource_id: '42',
      completed_at: '2026-09-19T20:00:00Z',
    }],
  })
  assert.equal(action.completedAt, '2026-09-19T20:00:00Z')
})
