/**
 * Keep mocked AskUserQuestion streams aligned with the durable chat protocol.
 *
 * A real question commit publishes pending_question_id through both chat detail
 * and /runtime until the answer POST commits. Tests that mock both the stream
 * and POST must mirror that state or a terminal refresh correctly turns their
 * transient question card into read-only history.
 */
import { testChatAgentSettings } from './_chatTestPrerequisites.mjs'
import { createMockChatRuntime } from './_mockChatRuntime.mjs'

export async function mockPendingQuestionState(page, questionId) {
  let pendingQuestionId = null
  let turnStarted = false
  const runtime = createMockChatRuntime()

  // Register this helper after the test's response mocks. Playwright invokes
  // the newest route first, so fallback preserves the existing POST response
  // while this observer establishes the ordering boundary for the question.
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route => {
    const request = route.request()
    if (request.method() === 'POST' && !turnStarted) {
      const body = request.postDataJSON()
      if (!body.answers) {
        turnStarted = true
        pendingQuestionId = questionId
        runtime.update({
          running: true,
          pending_question_id: pendingQuestionId,
        })
      }
    }
    return route.fallback()
  })

  const fulfillQuestionState = route => {
    const request = route.request()
    if (request.method() !== 'GET') {
      return route.fallback()
    }

    const path = new URL(request.url()).pathname
    const runtimeState = runtime.snapshot({
      pending_question_id: pendingQuestionId,
    })
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      json: path.endsWith('/runtime')
        ? runtimeState
        : runtime.detail({
            id: path.split('/').at(-1),
            messages: [],
            total: 0,
            offset: 0,
            provider: 'claude',
            ...testChatAgentSettings(),
          }),
    })
  }

  await page.route(
    /\/api\/chats\/[0-9a-f-]+(?:\?.*)?$/,
    fulfillQuestionState,
  )
  await page.route(
    /\/api\/chats\/[0-9a-f-]+\/runtime(?:\?.*)?$/,
    fulfillQuestionState,
  )

  return {
    markAnswered() {
      pendingQuestionId = null
      runtime.update({ pending_question_id: null })
    },
  }
}
