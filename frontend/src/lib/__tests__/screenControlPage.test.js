import test from 'node:test'
import assert from 'node:assert/strict'

import {
  boundedScreenCaptureSize,
  isSensitiveScreenControlField,
  parseScreenControlFrameRef,
} from '../screenControlPage.js'


test('live screen refs remain exact-frame and reject ambiguous shapes', () => {
  assert.deepEqual(parseScreenControlFrameRef('app:42:e7'), {
    appId: '42', ref: 'e7',
  })
  assert.equal(parseScreenControlFrameRef('e7'), null)
  assert.equal(parseScreenControlFrameRef('app:42:button'), null)
  assert.equal(parseScreenControlFrameRef('app:42:e7:extra'), null)
})


test('credential and payment fields stay sensitive while ordinary text does not', () => {
  assert.equal(isSensitiveScreenControlField('password', ''), true)
  assert.equal(isSensitiveScreenControlField('text', 'section-login current-password'), true)
  assert.equal(isSensitiveScreenControlField('text', 'one-time-code'), true)
  assert.equal(isSensitiveScreenControlField('text', 'cc-number'), true)
  assert.equal(isSensitiveScreenControlField('email', 'email'), false)
})


test('shared screenshots preserve aspect ratio under the response-size ceiling', () => {
  assert.deepEqual(boundedScreenCaptureSize(1440, 900), { width: 1440, height: 900 })
  assert.deepEqual(boundedScreenCaptureSize(5120, 2880), { width: 2560, height: 1440 })
  assert.deepEqual(boundedScreenCaptureSize(1170, 2532), { width: 1170, height: 2532 })
  assert.equal(boundedScreenCaptureSize(0, 900), null)
})
