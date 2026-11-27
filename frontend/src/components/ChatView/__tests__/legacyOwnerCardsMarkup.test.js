// Approval, Restart and Secure input cards saved before request-list cards
// existed keep rendering as they did: the same card structure and classes,
// header, question and option text, choice roles and accessible names,
// controls with their labels, and status lines. Icon artwork, Markdown
// wrappers and attributes that only tune editing are left out, and element
// ids are numbered in order of appearance, so a reference between elements
// must still point at the same element while the generated value may change.
import assert from 'node:assert/strict'
import { test } from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import QuestionCard from '../QuestionCard.jsx'
import SecureInputCard from '../SecureInputCard.jsx'

// One line per element: tag, classes, its numbered id and the attributes that
// matter to the owner or assistive technology; one line per text run.
const KEEP = /^(class|role|aria-[a-z]+|type|disabled|placeholder|title|multiple|required|data-question-index|data-secure-field|data-secure-masked)$/
const ID_REFS = new Set(['aria-labelledby', 'aria-describedby', 'for'])
const VOID = new Set(['input', 'br', 'img', 'hr'])
const FLATTEN = new Set(['standard-markdown md-blocks', 'md-paragraph'])
function outline(html) {
  const ids = new Map()
  for (const [, id] of html.matchAll(/\sid="([^"]*)"/g)) if (!ids.has(id)) ids.set(id, `#${ids.size + 1}`)
  const lines = []
  let depth = 0
  let skip = 0
  const stack = []
  for (const [, close, tag, attrs = '', slash, text] of html.matchAll(/<(\/?)([a-zA-Z0-9]+)((?:\s[^>]*?)?)(\/?)>|([^<]+)/g)) {
    if (text !== undefined) {
      if (!skip && text.trim()) lines.push(`${'  '.repeat(depth)}"${text.trim()}"`)
      continue
    }
    if (close) {
      const open = stack.pop()
      if (open === 'skip') skip--
      else if (open === 'line') depth--
      continue
    }
    const selfClosing = VOID.has(tag) || slash === '/'
    if (skip || tag === 'svg') {
      if (!selfClosing) { stack.push('skip'); skip++ }
      continue
    }
    const parsed = [...attrs.matchAll(/([a-zA-Z-]+)(?:="([^"]*)")?/g)].map(([, name, value = '']) => [name, value])
    const cls = parsed.find(([name]) => name === 'class')?.[1] || ''
    if (FLATTEN.has(cls)) { if (!selfClosing) stack.push('flat'); continue }
    const id = parsed.find(([name]) => name === 'id')?.[1]
    const shown = parsed.filter(([name]) => KEEP.test(name) && name !== 'class').map(([name, value]) => {
      const v = ID_REFS.has(name) ? value.split(' ').map(ref => ids.get(ref) || ref).join(' ') : value
      return v === '' ? name : `${name}="${v}"`
    })
    lines.push(`${'  '.repeat(depth)}${tag}${cls ? `.${cls.split(' ').join('.')}` : ''}${id ? ` ${ids.get(id)}` : ''}${shown.length ? ` ${shown.join(' ')}` : ''}`)
    if (!selfClosing) { stack.push('line'); depth++ }
  }
  return lines.join('\n')
}

const restartV2 = {
  version: 2, type: 'restart', action_id: 'platform-restart:a', restart_option_id: 'opt-r',
  requirement: {}, status: 'awaiting_owner', wait_id: 'activation-q',
}
const restartQuestions = [{
  id: 'restart', header: 'Restart Möbius', question: 'Restart Möbius now? This interrupts active turns.',
  options: [{ id: 'opt-r', label: 'Restart now', on_answer: 'close', description: 'Drain active work and restart once.' }],
}]
const approvalQuestions = [{
  id: 'approval', header: 'Approval', question: 'Publish the notes?',
  options: [{ id: '0', label: 'Publish', description: 'Post now.' }, { id: '1', label: 'Not now', description: 'Keep a draft.' }],
}]
const secure = {
  type: 'secure_input', request_id: 'sec-1', mode: 'sealed', title: 'Connect mail', description: 'Bypasses the model.',
  fields: [{ name: 'api_key', label: 'API key', type: 'password', autocomplete: 'off' }], status: 'pending',
}
const questionCases = {
  approvalOpen: { chatId: 'c', questionId: 'q-approval', questions: approvalQuestions, onAnswer: () => true },
  approvalAnswered: { chatId: 'c', questionId: 'q-approval', questions: approvalQuestions, answeredMap: { 'Publish the notes?': 'Publish' }, submittedOptions: { approval: ['0'] } },
  restartOpen: { chatId: 'c', questionId: 'q-restart', questions: restartQuestions, platformAction: restartV2, onAnswer: () => true },
  restartRequested: { chatId: 'c', questionId: 'q-restart', questions: restartQuestions, platformAction: { ...restartV2, status: 'restart_requested' }, answeredMap: { 'Restart Möbius now? This interrupts active turns.': 'Restart now' } },
  restartResponded: { chatId: 'c', questionId: 'q-restart', questions: restartQuestions, platformAction: { ...restartV2, status: 'responded' }, answeredMap: { 'Restart Möbius now? This interrupts active turns.': 'Not yet' }, submittedOptions: {} },
  groupedOrdinary: { chatId: 'c', questionId: 'q-g', questions: [{ id: 'a', question: 'First?', options: [{ id: 'x', label: 'X' }] }, { id: 'b', question: 'Second?', options: [] }], onAnswer: () => true },
}
const secureCases = {
  securePending: { block: secure, chatId: 'c', interactive: true },
  secureCompleted: { block: { ...secure, status: 'completed' }, chatId: 'c' },
  secureFailed: { block: { ...secure, status: 'failed', outcome: 'The sealed consumer failed.' }, chatId: 'c' },
  secureReveal: { block: { ...secure, mode: 'reveal' }, chatId: 'c', interactive: true },
}

const LABELS = {
  approvalOpen: 'open approval',
  approvalAnswered: 'answered approval',
  restartOpen: 'open restart',
  restartRequested: 'requested restart',
  restartResponded: 'responded restart',
  groupedOrdinary: 'grouped question',
  securePending: 'pending secure input',
  secureCompleted: 'completed secure input',
  secureFailed: 'failed secure input',
  secureReveal: 'reveal secure input',
}

const expected = {
  approvalOpen: `
div.qcard
  div.qcard__questions
    div.qcard__q data-question-index="0"
      div.qcard__header
        "Approval"
      div.qcard__text #1
        "Publish the notes?"
      div.qcard__hint
        "Choose one"
      div.qcard__opts role="radiogroup" aria-labelledby="#1"
        div.qcard__opt
          div.qcard__opt-choice role="radio" aria-checked="false" aria-describedby="#2"
            span.qcard__mark.qcard__mark--radio aria-hidden="true"
            span.qcard__opt-label
              "Publish"
          div.qcard__opt-desc #2
            "Post now."
        div.qcard__opt
          div.qcard__opt-choice role="radio" aria-checked="false" aria-describedby="#3"
            span.qcard__mark.qcard__mark--radio aria-hidden="true"
            span.qcard__opt-label
              "Not now"
          div.qcard__opt-desc #3
            "Keep a draft."
      div.qcard__answer-row
        button.qcard__attach #4 type="button" aria-label="Attach a photo or file" title="Attach a photo or file"
        div.qcard__composer
          div.qcard__answer-files role="group" aria-label="Files for this answer"
          textarea.qcard__input #5 aria-label="Custom answer for:" aria-labelledby="#5 #1" placeholder="Or type your own answer…"
  input.qcard__file-input type="file" multiple aria-label="Attach files to your answer"
  div.qcard__submit-hint.chat__sr-only role="status"
  button.qcard__submit.qcard__submit--incomplete type="button" aria-disabled="true"
    "Submit"`,
  approvalAnswered: `
div.qcard.qcard--answered
  div.qcard__questions
    div.qcard__q data-question-index="0"
      div.qcard__header
        "Approval"
      div.qcard__text #1
        "Publish the notes?"
      div.qcard__hint
        "Choose one"
      div.qcard__opts role="radiogroup" aria-labelledby="#1"
        div.qcard__opt.qcard__opt--on.qcard__opt--static
          div.qcard__opt-choice role="radio" aria-checked="true" aria-disabled="true" aria-describedby="#2"
            span.qcard__mark.qcard__mark--radio aria-hidden="true"
            span.qcard__opt-label
              "Publish"
          div.qcard__opt-desc #2
            "Post now."
        div.qcard__opt.qcard__opt--dim.qcard__opt--static
          div.qcard__opt-choice role="radio" aria-checked="false" aria-disabled="true" aria-describedby="#3"
            span.qcard__mark.qcard__mark--radio aria-hidden="true"
            span.qcard__opt-label
              "Not now"
          div.qcard__opt-desc #3
            "Keep a draft."
      div.qcard__answer-row
        button.qcard__attach #4 type="button" aria-label="Attach a photo or file" title="Attach a photo or file" disabled
        div.qcard__composer
          div.qcard__answer-files role="group" aria-label="Files for this answer"
          textarea.qcard__input #5 aria-label="Custom answer for:" aria-labelledby="#5 #1" placeholder="No custom answer"
  input.qcard__file-input type="file" multiple disabled aria-label="Attach files to your answer"
  div.qcard__submit-hint.chat__sr-only role="status"
  button.qcard__submit type="button" disabled
    "Submitted"`,
  restartOpen: `
div.qcard
  div.qcard__questions
    div.qcard__q data-question-index="0"
      div.qcard__header
        "Restart Möbius"
      div.qcard__text #1
        "Restart Möbius now? This interrupts active turns."
      div.qcard__hint
        "Restart now, or reply below"
      div.qcard__opts role="radiogroup" aria-labelledby="#1"
        div.qcard__opt
          div.qcard__opt-choice role="radio" aria-checked="false" aria-describedby="#2"
            span.qcard__mark.qcard__mark--radio aria-hidden="true"
            span.qcard__opt-label
              "Restart now"
          div.qcard__opt-desc #2
            "Drain active work and restart once."
      div.qcard__answer-row
        div.qcard__composer
          textarea.qcard__input #3 aria-label="Custom answer for:" aria-labelledby="#3 #1" placeholder="Or tell me what you’d like to do instead…"
  div.qcard__submit-hint.chat__sr-only role="status"
  button.qcard__submit type="button" disabled
    "Continue"`,
  restartRequested: `
div.qcard.qcard--answered
  div.qcard__action-status role="status"
    "Restart requested"
    span.qcard__action-detail
      "Möbius is draining active work and will restart once."
  div.qcard__questions
    div.qcard__q data-question-index="0"
      div.qcard__header
        "Restart Möbius"
      div.qcard__text #1
        "Restart Möbius now? This interrupts active turns."`,
  restartResponded: `
div.qcard.qcard--answered
  div.qcard__action-status role="status"
    "Response sent"
    span.qcard__action-detail
      "This card did not restart Möbius. The agent will respond to what you wrote instead."
  div.qcard__questions
    div.qcard__q data-question-index="0"
      div.qcard__header
        "Restart Möbius"
      div.qcard__text #1
        "Restart Möbius now? This interrupts active turns."
      div.qcard__answer-row
        div.qcard__composer.qcard__composer--active
          textarea.qcard__input #2 aria-label="Custom answer for:" aria-labelledby="#2 #1" placeholder="No custom answer"
            "Not yet"`,
  groupedOrdinary: `
div.qcard.qcard--grouped aria-label="2 decisions"
  div.qcard__group-head
    div
      div.qcard__group-title
        "2 decisions"
      div.qcard__group-copy
        "Answer each question, then submit them together."
    span.qcard__group-count
      "2"
  div.qcard__questions
    div.qcard__q data-question-index="0"
      div.qcard__text #1
        "First?"
      div.qcard__hint
        "Choose one"
      div.qcard__opts role="radiogroup" aria-labelledby="#1"
        div.qcard__opt
          div.qcard__opt-choice role="radio" aria-checked="false"
            span.qcard__mark.qcard__mark--radio aria-hidden="true"
            span.qcard__opt-label
              "X"
      div.qcard__answer-row
        button.qcard__attach #2 type="button" aria-label="Attach a photo or file to your answer to:" aria-labelledby="#2 #1" title="Attach a photo or file"
        div.qcard__composer
          div.qcard__answer-files role="group" aria-label="Files for this answer"
          textarea.qcard__input #3 aria-label="Custom answer for:" aria-labelledby="#3 #1" placeholder="Or type your own answer…"
    div.qcard__q data-question-index="1"
      div.qcard__text #4
        "Second?"
      div.qcard__answer-row
        button.qcard__attach #5 type="button" aria-label="Attach a photo or file to your answer to:" aria-labelledby="#5 #4" title="Attach a photo or file"
        div.qcard__composer
          div.qcard__answer-files role="group" aria-label="Files for this answer"
          textarea.qcard__input #6 aria-label="Custom answer for:" aria-labelledby="#6 #4" placeholder="Type your answer…"
  input.qcard__file-input type="file" multiple aria-label="Attach files to your answer"
  div.qcard__submit-hint.chat__sr-only role="status"
  button.qcard__submit.qcard__submit--incomplete type="button" aria-disabled="true"
    "Submit"`,
  securePending: `
section.secure-card aria-label="Connect mail"
  header.secure-card__head
    span.secure-card__lock aria-hidden="true"
    div
      h3.secure-card__title
        "Connect mail"
  p.secure-card__description
    "Bypasses the model."
  form.secure-card__form
    label.secure-card__field
      span
        "API key"
      input type="text" required data-secure-field="api_key" data-secure-masked="true"
    div.secure-card__actions
      button.secure-card__cancel type="button"
        "Cancel"
      button.secure-card__submit type="submit"
        "Enter securely"
  p.secure-card__foot
    "One-time entry · values bypass the chat and AI"`,
  secureCompleted: `
section.secure-card aria-label="Connect mail"
  header.secure-card__head
    span.secure-card__lock aria-hidden="true"
    div
      h3.secure-card__title
        "Connect mail"
  p.secure-card__description
    "Bypasses the model."
  div.secure-card__receipts role="status"
    div.secure-card__receipt
      span.secure-card__receipt-prompt
        span.secure-card__receipt-lock aria-hidden="true"
        span
          "API key"
      strong
        "Provided securely"
  p.secure-card__foot
    "Receipt saved · entered values omitted"`,
  secureFailed: `
section.secure-card aria-label="Connect mail"
  header.secure-card__head
    span.secure-card__lock aria-hidden="true"
    div
      h3.secure-card__title
        "Connect mail"
  p.secure-card__description
    "Bypasses the model."
  div.secure-card__receipts role="status"
    div.secure-card__receipt
      span.secure-card__receipt-prompt
        span.secure-card__receipt-lock aria-hidden="true"
        span
          "API key"
      strong
        "Used securely"
  p.secure-card__description role="status"
    "The sealed consumer failed."
  p.secure-card__foot
    "Operation failed · entered values omitted"`,
  secureReveal: `
section.secure-card.secure-card--reveal aria-label="Connect mail"
  header.secure-card__head
    span.secure-card__lock aria-hidden="true"
    div
      h3.secure-card__title
        "Connect mail"
  p.secure-card__description
    "Bypasses the model."
  form.secure-card__form
    label.secure-card__field
      span
        "API key"
      input type="text" required data-secure-field="api_key" data-secure-masked="true"
    label.secure-card__consent
      input type="checkbox"
      span
        "I understand these values will be sent to the AI provider. Möbius will omit them from its own chat and logs."
    div.secure-card__actions
      button.secure-card__cancel type="button"
        "Cancel"
      button.secure-card__submit type="submit"
        "Reveal for this turn"
  p.secure-card__foot
    "Explicit reveal sends values to AI; Möbius omits them from its transcript."`,
}

for (const [name, props] of Object.entries(questionCases)) {
  test(`legacy ${LABELS[name]} card markup is unchanged`, () => {
    assert.equal(outline(renderToStaticMarkup(createElement(QuestionCard, props))), expected[name].trim())
  })
}
for (const [name, props] of Object.entries(secureCases)) {
  test(`legacy ${LABELS[name]} card markup is unchanged`, () => {
    assert.equal(outline(renderToStaticMarkup(createElement(SecureInputCard, props))), expected[name].trim())
  })
}
