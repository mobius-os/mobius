import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'

const component = readFileSync(new URL('../QuestionCard.jsx', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../QuestionCard.css', import.meta.url), 'utf8')

test('question option explanations remain selectable without choosing them', () => {
  const optionRule = css.match(/\.qcard__opt\s*\{[^}]*\}/s)?.[0] || ''

  assert.match(optionRule, /user-select:\s*text/)
  assert.match(optionRule, /-webkit-user-select:\s*text/)
  assert.match(optionRule, /-webkit-touch-callout:\s*default/)
  assert.match(component, /const OptionSurface = inactive \? 'div' : 'button'/,
    'answered or disabled options should become static selectable content')
  assert.match(
    component,
    /event\.detail !== 0[\s\S]*pointerSelectionChangedWithin\([\s\S]*event\.currentTarget[\s\S]*\) return[\s\S]*selectOption/,
    'a pointer selection should not also choose the live option',
  )
})

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
  assert.doesNotMatch(component, /qcard__opt--other/,
    'a custom answer should be a direct writing surface, not an Other option')
  assert.match(component, /<CustomAnswerArea[\s\S]*?answered=\{answered\}[\s\S]*?value=\{answered[\s\S]*?unmatchedAnswers\.join\(', '\)/,
    'the custom answer should stay mounted and retain submitted custom text')
  assert.match(component, /rows=\{1\}/,
    'the custom answer should begin as one compact writing line')
  assert.match(component, /data-chat-inline-editor="question-answer"/,
    'the scroll controller should recognize the editor through a semantic marker')
  assert.match(component, /onFocus=\{e => placeCaretAtTextEnd\(e\.currentTarget\)\}/,
    'returning to a custom answer should put the caret after its saved text')
  assert.match(component, /readOnly=\{answered\}[\s\S]*?disabled=\{disabled && !answered\}/,
    'a submitted multiline answer should stay scrollable but not editable')
  assert.match(component, /resizeCustomAnswer|textareaUsesNativeSizing/,
    'older browsers should measure the growing answer when native sizing is unavailable')
  assert.match(component, /e\.key === 'Enter' && \(e\.metaKey \|\| e\.ctrlKey\)/,
    'plain Enter should create a new line while the explicit shortcut submits')
  assert.match(component, /val\.replace\(\/\\n\/g, '\\n  '\)/,
    'multiline custom answers should keep their structure in the resumed turn')
  assert.match(component, /const next = arr\.includes\(label\)[\s\S]*?: \[\.\.\.arr, label\]/,
    'multi-select options should compose with a written custom answer')
  assert.match(component, /if \(!q\?\.multiSelect\) \{\s*setOtherTexts\(prev => \(\{ \.\.\.prev, \[question\]: '' \}\)\)/,
    'choosing a single option should clear custom text that is no longer active')
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
  assert.match(css, /\.qcard__input:disabled,\s*\.qcard__input\[readonly\]\s*\{[\s\S]*?color:\s*var\(--muted\);[\s\S]*?-webkit-text-fill-color:\s*var\(--muted\);[\s\S]*?\}/,
    'a submitted custom answer should visibly gray out in every browser')
  assert.match(css, /\.qcard__input\s*\{[\s\S]*?width:\s*100%;[\s\S]*?min-height:\s*38px;[\s\S]*?font-size:\s*13px;[\s\S]*?field-sizing:\s*content;[\s\S]*?max-height:\s*180px;[\s\S]*?overflow-y:\s*auto;[\s\S]*?resize:\s*none;/,
    'the custom answer should expand inline to a bounded, internally scrollable height')
  assert.match(css, /\.qcard__submit-error\s*\{/,
    'a failed answer should keep its retry notice attached to the card')
})

test('multiple questions read as one compact decision panel', () => {
  assert.match(component, /const grouped = questions\.length > 1/)
  assert.match(component, /className=\{`qcard\$\{grouped \? ' qcard--grouped'/)
  assert.match(component, /\{questions\.length\} decisions/)
  assert.match(component, /Choose each one, then submit them together\./)
  assert.match(css, /\.qcard\s*\{[\s\S]*?width:\s*min\(100%, 640px\);[\s\S]*?margin:\s*10px auto;/)
  assert.match(css, /\.qcard--grouped\s*\{[\s\S]*?overflow:\s*hidden;/)
  assert.match(css, /\.qcard--grouped \.qcard__q \+ \.qcard__q\s*\{[\s\S]*?margin-top:\s*0;/)
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

test('a pending question exposes Stop instead of an impossible steer', () => {
  assert.match(
    chatView,
    /const showSteer = !hasPendingQuestion[\s\S]*?const canSteer = canRequestSteer[\s\S]*?canFastForwardQueue/,
    'the composer must fall back to Stop while request_user_input owns the turn',
  )
  assert.match(
    chatView,
    /steerActive=\{turnActive && !hasPendingQuestion\}/,
    'queued rows must not offer per-row steer while the live question blocks it',
  )
})
