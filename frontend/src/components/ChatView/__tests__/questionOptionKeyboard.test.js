/* Tapping an answer while typing must not close the phone keyboard: a touch
   press on an option leaves focus in the text field (the click still picks),
   while a mouse press keeps its default so description text stays selectable. */
import { after, afterEach, test } from 'node:test'
import assert from 'node:assert/strict'
import { act } from 'react'
import { click, h, mount, unmountAll } from './mountedRender.js'
import { createSsrRenderServer } from '../../../lib/__tests__/ssrRenderServer.mjs'

const vite = await createSsrRenderServer()
after(() => vite.close())
const { default: QuestionCard } = await vite.ssrLoadModule('/src/components/ChatView/QuestionCard.jsx')
afterEach(() => { unmountAll(); document.activeElement = document.body })

const QUESTIONS = [{ header: 'Pick', question: 'Which one?', options: [{ label: 'A', description: 'First' }, { label: 'B', description: 'Second' }] }]

function press(element, pointerType) {
  const propsKey = Object.keys(element).find(key => key.startsWith('__reactProps$'))
  let prevented = false
  act(() => {
    element[propsKey].onPointerDown({ pointerType, currentTarget: element, target: element, preventDefault() { prevented = true } })
  })
  return prevented
}
const option = view => view.container.querySelectorAll('div').find(node => String(node.className).split(' ').includes('qcard__opt'))

test('a touch tap on an answer keeps the typing field focused', () => {
  const view = mount(h(QuestionCard, { chatId: 'c', questionId: 'q', questions: QUESTIONS, onAnswer: () => {} }))
  document.activeElement = view.container.querySelector('textarea')
  assert.equal(press(option(view), 'touch'), true, 'touch press must not take focus from the field')
  assert.equal(press(option(view), 'mouse'), false, 'mouse press keeps its default')
})

test('the tap that keeps focus still picks the answer', () => {
  const view = mount(h(QuestionCard, { chatId: 'c', questionId: 'q', questions: QUESTIONS, onAnswer: () => {} }))
  document.activeElement = view.container.querySelector('textarea')
  const choice = option(view).querySelector('[role="radio"]')
  press(option(view), 'touch')
  click(choice)
  assert.equal(choice.getAttribute('aria-checked'), 'true')
})

test('with no field focused, a touch tap behaves normally', () => {
  const view = mount(h(QuestionCard, { chatId: 'c', questionId: 'q', questions: QUESTIONS, onAnswer: () => {} }))
  assert.equal(press(option(view), 'touch'), false)
})
