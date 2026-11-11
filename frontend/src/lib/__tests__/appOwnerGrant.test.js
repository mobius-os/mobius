import test from 'node:test'
import assert from 'node:assert/strict'
import { beginAppOwnerGrant } from '../appOwnerGrant.js'
import {
  attributedFrameVersion, serveAppOwnerGrantRequest,
} from '../../components/AppCanvas/appFrameProtocol.js'

test('owner grant request takes its app id from the shell and never occupies the slot from a hidden or stale frame', () => {
  const replies = []
  const live = { postMessage: message => replies.push(message) }
  const buffered = { postMessage: message => replies.push(message) }
  const frames = new Map([[1, { contentWindow: live }], [2, { contentWindow: buffered }]])
  const message = {
    type: 'moebius:owner-grant', requestId: 'g1', action: 'merge', appId: 999,
    scope: { repo: 'example/project', number: '123' }, body: { method: 'squash' },
  }
  const begin = (source, visible) => beginAppOwnerGrant({
    message, source, visible, pending: null, appId: 12,
    sourceVersion: attributedFrameVersion(frames, source), liveVersion: 1,
  })

  const request = begin(live, true)
  assert.equal(request.appId, 12) // the forged appId in the payload is ignored
  assert.deepEqual([request.action, request.scope, request.body],
    ['merge', message.scope, message.body])
  // An accepted request only opens the sheet and is acked, so the frame stops its short wait.
  assert.deepEqual(replies.splice(0).map(reply => [reply.type, reply.requestId]), [['moebius:owner-grant-ack', 'g1']])

  assert.equal(begin(live, false), null) // hidden
  assert.equal(begin(buffered, true), null) // buffered successor, not the live frame
  assert.deepEqual(replies.map(reply => [reply.type, reply.cancelled]),
    [['moebius:owner-grant-result', true], ['moebius:owner-grant-result', true]])

  const pendingRef = { current: null }
  assert.equal(serveAppOwnerGrantRequest({
    message, source: live, frames, liveVersion: 1, visible: false, pendingRef, appId: 12,
    setPending: () => { throw new Error('a hidden frame must not open the sheet') },
  }), true)
  assert.equal(pendingRef.current, null)
  assert.equal(replies.at(-1).cancelled, true)
})
