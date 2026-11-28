// A request-list card streamed at the end of a turn is drawn from the live
// stream items and then promoted into the durable reply. The live and
// promoted blocks both keep `input_requests`, so the card is never redrawn on
// the next transcript read.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { streamItemToBlock, streamItemsToAssistantPayload } from '../streamPromotion.js'

const listCard = {
  type: 'question', question_id: 'card-1', input_requests: true, response_mode: 'continuation',
  questions: [{ id: 'r1', kind: 'question', question: 'How formal?', options: [] }],
  request_states: { r2: { status: 'consuming' } },
  selected_options: { r1: [] },
}

test('a live list card keeps its list fields on the live surface and when promoted', () => {
  for (const finalize of [false, true]) {
    const [block] = streamItemsToAssistantPayload([listCard], { finalize }).blocks
    assert.equal(block.input_requests, true, `finalize=${finalize}`)
    assert.deepEqual(block.request_states, listCard.request_states)
    assert.deepEqual(block.selected_options, listCard.selected_options)
    assert.equal(block.response_mode, 'continuation')
  }
})

test('an ordinary card gains nothing it did not have', () => {
  assert.deepEqual(
    Object.keys(streamItemToBlock({ type: 'question', question_id: 'q', questions: listCard.questions })).sort(),
    ['question_id', 'questions', 'type'],
  )
})
