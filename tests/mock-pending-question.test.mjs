import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mockPendingQuestionState } from './_mockPendingQuestion.mjs'

test('question projections share one lifecycle revision until the modeled transition', async () => {
  const routes = []
  const question = await mockPendingQuestionState({
    route: async (pattern, handler) => routes.push({ pattern, handler }),
  }, 'question-a')
  const request = async (suffix, method = 'GET', body = {}) => {
    const url = `http://fixture/api/chats/abcdef${suffix}`
    const handler = routes.find(route => route.pattern.test(url)).handler
    let json
    await handler({
      request: () => ({ method: () => method, url: () => url, postDataJSON: () => body }),
      fallback: () => {},
      fulfill: response => { json = response.json },
    })
    return json
  }
  assert.equal((await request('')).runtime_revision, 0)
  assert.equal((await request('/runtime')).runtime_revision, 0)
  await request('/messages', 'POST', { content: 'Ask me' })
  const detail = await request('')
  const runtime = await request('/runtime')
  assert.equal(detail.runtime_revision, 1)
  assert.equal(runtime.runtime_revision, 1)
  assert.equal(runtime.pending_question_id, 'question-a')
  assert.equal((await request('/runtime')).runtime_revision, 1)
  question.markAnswered()
  const answered = await request('/runtime')
  assert.equal(answered.runtime_revision, 2)
  assert.equal(answered.pending_question_id, null)
  question.markAnswered()
  assert.equal((await request('')).runtime_revision, 2)
  // A response captured before the answer retains its earlier cursor.
  assert.equal(detail.runtime_revision, 1)
  assert.equal(detail.pending_question_id, 'question-a')
})
