import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const component = readFileSync(new URL('../QuestionCard.jsx', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../QuestionCard.css', import.meta.url), 'utf8')

test('unanswered question cards do not have a stale gray state', () => {
  assert.doesNotMatch(component, /const stale = disabled && !answered/,
    'QuestionCard should not model unanswered questions as stale')
  assert.doesNotMatch(component, /qcard--stale/,
    'unanswered cards should not receive a stale visual class')
  assert.doesNotMatch(component, /This question is no longer active/,
    'unanswered cards should not tell the user the question expired')
  assert.match(component, /\{\(answered \|\| !disabled\) && \([\s\S]*<button[\s\S]*className="qcard__submit"/,
    'submit button should remain in place after an answer is submitted')
  assert.match(component, /submitting \? 'Submitting…' : \(answered \? 'Submitted' : 'Submit'\)/,
    'the retained submit button should explain pending and answered states')
  assert.match(component, /\{\(!disabled \|\| answered\) && \(\s*<div className="qcard__hint"/,
    'selection hints should stay in place after the answer is submitted')
  assert.doesNotMatch(component, /\{!answered && \(\s*<button[\s\S]*?qcard__opt--other/,
    'the Other option should not disappear after submission')
  assert.match(component, /\{\(isOtherSelected \|\| answeredWithOther\) && \(\s*<input/,
    'a submitted custom answer should keep its input row in place')
  assert.match(component, /writeQuestionDraft\(draftKey, answers, otherTexts\)/,
    'unsubmitted selections and custom text should be cached')
  assert.match(component, /if \(answered\) \{\s*clearQuestionDraft\(draftKey\)/,
    'committed answers should clear their cached draft')
  assert.doesNotMatch(component, /if \(answered \|\| disabled\) \{\s*clearQuestionDraft/,
    'a transient disabled handoff must not erase an offline choice')
  assert.match(component, /Your choice is saved — submit it when you’re back online/,
    'an offline submit should explain that the choice is retained')
  assert.match(component, /const accepted = await onAnswer[\s\S]*if \(accepted !== false\) setSubmitted\(true\)/,
    'a card should settle only after the answer request is accepted')
  assert.match(component, /catch \{[\s\S]*Keep the choices and[\s\S]*\} finally/,
    'a failed answer should retain its retryable draft')
})

test('question card css has no stale styling hook', () => {
  assert.doesNotMatch(css, /\.qcard--stale\s*\{[\s\S]*?\}/,
    'stale question styling should not come back')
  assert.doesNotMatch(css, /\.qcard__status\s*\{[\s\S]*?\}/,
    'expiration status styling should not come back')
  assert.match(css, /\.qcard__input:disabled\s*\{[\s\S]*?color:\s*var\(--muted\);[\s\S]*?-webkit-text-fill-color:\s*var\(--muted\);[\s\S]*?\}/,
    'a submitted custom answer should visibly gray out in every browser')
  assert.match(css, /\.qcard__submit-error\s*\{/,
    'a failed answer should keep its retry notice attached to the card')
})

test('a failed question submission does not append a transcript row', () => {
  const start = chatView.indexOf('const doSendSilent = useCallback')
  const end = chatView.indexOf('function handleSubmit(e)', start)
  assert.ok(start >= 0 && end > start, 'doSendSilent source should be present')
  const silentSubmit = chatView.slice(start, end)
  assert.doesNotMatch(
    silentSubmit,
    /content: `Error:/,
    'a transient answer failure must stay on the card, not supersede it',
  )
  assert.match(silentSubmit, /QuestionCard owns this transient failure notice/)
})

test('question submission paints a resumed turn only after the POST commits', () => {
  const start = chatView.indexOf('const doSendSilent = useCallback')
  const end = chatView.indexOf('function handleSubmit(e)', start)
  const silentSubmit = chatView.slice(start, end)
  const send = silentSubmit.indexOf('const response = await streamSend')
  const paintRunning = silentSubmit.indexOf('setServerRunningState(true)')

  assert.ok(send >= 0 && paintRunning > send,
    'a pending answer must not remount the durable question card')
  assert.match(silentSubmit, /sendingRef\.current = wasSending/,
    'a failed answer must restore the synchronous composer guard')
  assert.match(silentSubmit, /setServerRunningState\(wasServerRunning\)/,
    'a failed answer must restore the prior durable running verdict')
})

test('question submission freezes the visible anchor before the async handoff', () => {
  const start = chatView.indexOf('const doSendSilent = useCallback')
  const end = chatView.indexOf('function handleSubmit(e)', start)
  const silentSubmit = chatView.slice(start, end)
  const freeze = silentSubmit.indexOf('freezeQuestionSubmission()')
  const send = silentSubmit.indexOf('const response = await streamSend')

  assert.ok(freeze >= 0 && send > freeze,
    'the reader anchor must freeze synchronously before answer delivery resumes output')
})
