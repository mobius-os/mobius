/* GitHub share links must never claim to upload or grant access to local files. */
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { githubCollaborationLink } from '../projectGithubSharing.js'

test('a connected repository gives recipients the GitHub fork choice', () => {
  assert.equal(githubCollaborationLink({ connected: true, repository: 'owner/project' }), 'https://github.com/owner/project/fork')
})
test('missing or malformed connections never produce a link', () => {
  for (const status of [null, {}, { connected: false, repository: 'owner/project' },
    ...['https://evil.test/x', 'owner/../evil', 'owner/project?x=1', '../project', 'owner/project#x', 'owner/'].map(repository => ({ connected: true, repository }))]) {
    assert.equal(githubCollaborationLink(status), null)
  }
})
test('unpublished changes do not change the shared GitHub destination', () => {
  assert.equal(githubCollaborationLink({ connected: true, repository: 'owner/my.project', dirty: true, ahead: 3 }), 'https://github.com/owner/my.project/fork')
})
