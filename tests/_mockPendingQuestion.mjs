/**
 * Keep mocked AskUserQuestion streams aligned with the durable chat protocol.
 *
 * A real question commit publishes pending_question_id through both chat detail
 * and /runtime until the answer POST commits. Tests that mock both the stream
 * and POST must mirror that state or a terminal refresh correctly turns their
 * transient question card into read-only history.
 */
import { testChatAgentSettings } from './_chatTestPrerequisites.mjs'

export async function mockPendingQuestionState(page, questionId) {
  let pendingQuestionId = null
  let turnStarted = false
  let runtimeRevision = 0
  let messages = []

  // Register this helper after the test's response mocks. Playwright invokes
  // the newest route first, so fallback preserves the existing POST response
  // while this observer establishes the ordering boundary for the question.
  await page.route(/\/api\/chats\/[0-9a-f-]+\/messages$/, route => {
    const request = route.request()
    if (request.method() === 'POST' && !turnStarted) {
      const body = request.postDataJSON()
      if (!body.answers) {
        turnStarted = true
        runtimeRevision += 1
        pendingQuestionId = questionId
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
    const runtime = {
      runtime_revision: runtimeRevision,
      running: false,
      active_goal_objective: null,
      pending_messages: [],
      pending_question_id: pendingQuestionId,
      updated_at: null,
    }
    return route.fulfill({
      status: 200,
      contentType: 'application/json',
      json: path.endsWith('/runtime')
        ? runtime
        : {
            ...runtime,
            id: path.split('/').at(-1),
            messages,
            total: messages.length,
            offset: 0,
            provider: 'claude',
            ...testChatAgentSettings(),
          },
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
    markAnswered({ messages: answeredMessages } = {}) {
      // Background delivery has no interactive caller to patch its card. Its
      // authoritative read must include the committed answered question.
      if (answeredMessages) messages = structuredClone(answeredMessages)
      if (pendingQuestionId !== null) runtimeRevision += 1
      pendingQuestionId = null
    },
  }
}
