import { readFileSync } from 'node:fs'
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import QuestionCard from '../QuestionCard.jsx'
import { LocalAnswersContext } from '../localAnswersContext.js'
import { questionDraftKey, writeQuestionDraft } from '../questionDraft.js'

const component = readFileSync(new URL('../QuestionCard.jsx', import.meta.url), 'utf8')
const chatView = readFileSync(new URL('../ChatView.jsx', import.meta.url), 'utf8')
const css = readFileSync(new URL('../QuestionCard.css', import.meta.url), 'utf8')

test('the paperclip is an icon left of the last answer box, not a row below', () => {
  const single = renderToStaticMarkup(createElement(QuestionCard, {
    chatId: 'clip', questionId: 'clip-q', questions: [{ question: 'Anything else?', options: [] }],
  }))
  assert.match(single, /class="qcard__answer-row"><input[^>]*class="qcard__file-input"[^>]*\/><button[^>]*class="qcard__attach"[\s\S]*?<\/button><div class="qcard__composer/)
  assert.doesNotMatch(single, /attach or paste/)
  const grouped = renderToStaticMarkup(createElement(QuestionCard, {
    chatId: 'clip', questionId: 'clip-g', questions: [
      { question: 'First?', options: [] },
      { question: 'Second?', options: [] },
    ],
  }))
  assert.equal((grouped.match(/class="qcard__attach"/g) || []).length, 1)
  assert.ok(grouped.indexOf('class="qcard__attach"') > grouped.indexOf('Second?'))
})

test('a file-only question answer can submit and ordinary cards offer upload', () => {
  const storage = {
    values: new Map(),
    getItem(key) { return this.values.get(key) || null },
    setItem(key, value) { this.values.set(key, value) },
    removeItem(key) { this.values.delete(key) },
  }
  const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'localStorage')
  Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: storage })
  try {
    const questions = [{ question: 'Send a picture', options: [] }]
    const key = questionDraftKey('file-only', 'file-only-q', questions)
    writeQuestionDraft(key, { answers: {}, otherTexts: {}, files: [{ name: 'photo.png', status: 'done', size: 4, mime_type: 'image/png' }] }, storage)
    const html = renderToStaticMarkup(createElement(QuestionCard, {
      chatId: 'file-only', questionId: 'file-only-q', questions,
    }))
    assert.match(html, /Attach a photo or file/)
    assert.match(html, /class="qcard__submit"[^>]*>Submit</)
    assert.equal((html.match(/aria-label="Files for this answer"/g) || []).length, 1)
    const submitted = renderToStaticMarkup(createElement(QuestionCard, {
      chatId: 'file-only', questionId: 'file-only-q', questions,
      answeredMap: { 'Send a picture': 'Attached 1 file' },
      attachments: [{ name: 'photo.png', size: 4, mime_type: 'image/png' }],
    }))
    assert.match(submitted, /aria-label="Files for this answer"/)
    assert.match(submitted, /chat__attachments/)
    // The attach row stays with the Submitted action row, so answering never
    // moves the card; it only stops taking files.
    assert.match(submitted, /class="qcard__attach"[^>]*disabled=""/)
    assert.match(submitted, /class="qcard__file-input"[^>]*disabled=""/)
    const restart = renderToStaticMarkup(createElement(QuestionCard, {
      chatId: 'restart', questionId: 'restart-q', questions,
      platformAction: { type: 'restart', version: 2, status: 'awaiting_owner' },
    }))
    assert.doesNotMatch(restart, /Attach a photo or file/)
  } finally {
    if (descriptor) Object.defineProperty(globalThis, 'localStorage', descriptor)
    else delete globalThis.localStorage
  }
})

test('text-only questions never offer choice instructions or empty choice groups', () => {
  for (const options of [undefined, null, []]) {
    for (const multiSelect of [false, true]) {
      for (const answeredMap of [undefined, { 'Which chat?': 'Example chat title' }]) {
        const html = renderToStaticMarkup(createElement(QuestionCard, {
          chatId: 'text-only', questionId: 'text-only-q', answeredMap,
          questions: [{ question: 'Which chat?', options, multiSelect }],
        }))
        assert.doesNotMatch(html, /Choose one|Select all that apply|qcard__hint|qcard__opts|radiogroup/)
        if (answeredMap) assert.match(html, /Example chat title/)
        else assert.match(html, /placeholder="Type your answer…"/)
      }
    }
  }
})

test('questions with choices retain single and multi-select instructions', () => {
  for (const [multiSelect, hint, role] of [[false, 'Choose one', 'radio'], [true, 'Select all that apply', 'checkbox']]) {
    const html = renderToStaticMarkup(createElement(QuestionCard, {
      chatId: 'choices', questionId: 'choices-q',
      questions: [{ question: 'Which?', options: [{ label: 'A' }, { label: 'B' }], multiSelect }],
    }))
    assert.ok(html.includes(hint))
    assert.ok(html.includes(`role="${role}"`))
    assert.match(html, /placeholder="Or type your own answer…"/)
  }
})

test('text-only replies retain exact punctuation and lines when submitted or queued', () => {
  const question = 'Describe the next step'
  const reply = 'First,  keep these spaces, \nthen keep this line.\nAnd this one.'
  for (const multiSelect of [false, true]) {
    for (const queued of [false, true]) {
      const records = queued ? [{
        chatId: 'written', body: { question_id: 'written-q', answers: { [question]: reply } },
      }] : []
      const html = renderToStaticMarkup(createElement(LocalAnswersContext.Provider, { value: records },
        createElement(QuestionCard, {
          chatId: 'written', questionId: 'written-q',
          questions: [{ question, multiSelect, options: [] }],
          answeredMap: queued ? undefined : { [question]: reply },
        })))
      assert.ok(html.includes(`>${reply}</textarea>`), 'a writing-only field must not parse text as a list of choices')
      assert.match(html, /readOnly=""/)
      assert.doesNotMatch(html, /qcard__hint|qcard__opts/)
    }
  }
})

test('mixed grouped questions describe answering without inventing text-only choices', () => {
  const html = renderToStaticMarkup(createElement(QuestionCard, {
    chatId: 'mixed', questionId: 'mixed-q',
    questions: [
      { question: 'Describe the next step', options: [] },
      { question: 'Which direction?', options: [{ label: 'Left' }, { label: 'Right' }] },
    ],
  }))
  assert.match(html, /Answer each question, then submit them together\./)
  assert.equal((html.match(/class="qcard__hint"/g) || []).length, 1)
  assert.equal((html.match(/role="radiogroup"/g) || []).length, 1)
  assert.match(html, /placeholder="Type your answer…"/)
  assert.doesNotMatch(html, /questions still need|questions to submit|qcard__submit-hint/)
})

test('a ten-question block renders every prompt and preserves every saved reply', () => {
  const questions = Array.from({ length: 10 }, (_, index) => ({
    id: `question-${index + 1}`, header: `Question ${index + 1}`,
    question: `Decision ${index + 1}?`, options: [],
  }))
  for (const answered of [false, true]) {
    const answeredMap = answered
      ? Object.fromEntries(questions.map((question, index) => [question.question, `Reply ${index + 1}`]))
      : undefined
    const html = renderToStaticMarkup(createElement(QuestionCard, {
      chatId: 'ten-questions', questionId: 'ten-questions-q', questions, answeredMap,
    }))
    assert.match(html, /10 decisions/)
    assert.equal((html.match(/<textarea\b/g) || []).length, 10)
    for (let index = 1; index <= 10; index += 1) {
      assert.ok(html.includes(`Decision ${index}?`))
      if (answered) assert.ok(html.includes(`>Reply ${index}</textarea>`))
    }
  }
})

test('a written Restart card uses its displayed action list for choice instructions', () => {
  const html = renderToStaticMarkup(createElement(QuestionCard, {
    chatId: 'filtered-restart', questionId: 'filtered-restart-q',
    platformAction: { type: 'restart', version: 2, status: 'awaiting_owner', restart_option_id: 'restart' },
    questions: [{ question: 'Restart?', options: [{ id: 'retired', label: 'Retired action' }] }],
  }))
  assert.doesNotMatch(html, /qcard__hint|qcard__opts|Retired action/)
  assert.match(html, /Or tell me what you’d like to do instead…/)
})

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
  assert.match(component, /\{!completedAction && \(answered \|\| !disabled\) && \([\s\S]*<button[\s\S]*className="qcard__submit"/,
    'submit button should remain in place after an answer is submitted')
  assert.match(component, /let submitLabel = writtenRestartAction \? 'Continue' : 'Submit'[\s\S]*if \(answered\) submitLabel = 'Submitted'[\s\S]*if \(submitting\) submitLabel = 'Submitting…'/,
    'the retained submit button should explain pending and answered states without rewriting legacy Restart cards')
  assert.match(component, /\{!completedAction && \(!disabled \|\| answered\) && hasOptions && \(\s*<div className="qcard__hint"/,
    'selection hints should stay in place after submission only when there are choices')
  assert.doesNotMatch(component, /qcard__opt--other/,
    'a custom answer should be a direct writing surface, not an Other option')
  assert.match(component, /const writtenAnswer = writtenRestartResponse \|\| !hasOptions[\s\S]*?unmatchedAnswers\.join\(', '\)[\s\S]*?<CustomAnswerArea[\s\S]*?answered=\{selectionLocked\}[\s\S]*?value=\{selectionLocked[\s\S]*?writtenAnswer/,
    'the custom answer should stay mounted and retain submitted custom text, including a written Restart response that matches an option label')
  assert.match(component, /writtenRestartAction[\s\S]*\? 'Or tell me what you’d like to do instead…'/,
    'a version-2 Restart card should replace Not now with a written response')
  assert.match(component, /writtenRestartAction[\s\S]*options\.filter\(opt => opt\.id === platformAction\.restart_option_id\)/,
    'only a version-2 Restart card should show just its exact Restart now option')
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
  assert.match(component, /isInlineEditorSubmit\(e, \{ isTouchPrimary: isTouchPrimary\(\) \}\)/,
    'the custom answer should send on the same Enter chord as the composer')
  assert.match(component, /if \(!canSubmit\) return/,
    'Enter should stay a newline until the card can actually be submitted')
  assert.match(component, /val\.replace\(\/\\n\/g, '\\n  '\)/,
    'multiline custom answers should keep their structure in the resumed turn')
  assert.match(component, /const next = arr\.includes\(label\)[\s\S]*?: \[\.\.\.arr, label\]/,
    'multi-select options should compose with a written custom answer')
  assert.match(component, /if \(!q\?\.multiSelect\) \{\s*setOtherTexts\(prev => \(\{ \.\.\.prev, \[question\]: '' \}\)\)/,
    'choosing a single option should clear custom text that is no longer active')
  assert.match(component, /writeQuestionDraft\(draftKey, \{ answers, otherTexts, files \}\)/,
    'unsubmitted selections, custom text, and files should be cached')
  assert.match(component, /if \(answered\) \{\s*clearQuestionDraft\(draftKey\)/,
    'committed answers should clear their cached draft')
  assert.doesNotMatch(component, /if \(answered \|\| disabled\) \{\s*clearQuestionDraft/,
    'a transient disabled handoff must not erase an offline choice')
  assert.match(component, /Your choice is saved — submit it when you’re back online/,
    'an offline submit should explain that the choice is retained')
  assert.match(component, /catch \(error\) \{[\s\S]*Keep the choices and[\s\S]*\} finally/,
    'a failed answer should retain its retryable draft')
})

test('question cards wrap long unbroken content within a mobile pane', () => {
  assert.match(css, /\.qcard\s*\{[^}]*overflow-wrap:\s*anywhere/,
    'long unbroken question and option text should not widen the card on mobile')
  assert.match(css, /\.qcard__input\s*\{[^}]*min-width:\s*0;[^}]*overflow-wrap:\s*anywhere;[^}]*word-break:\s*break-word;/s,
    'a long pasted answer should wrap within its textarea, not widen the chat')
  assert.match(component, /<textarea[\s\S]*?wrap="soft"[\s\S]*?value=\{value\}/,
    'custom answers should retain soft-wrapped text without inserting newlines into the submitted URL')
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
  assert.match(component, /Answer each question, then submit them together\./)
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
  assert.match(
    silentSubmit,
    /isQuestionStateChangedError\(err\)[\s\S]*await fetchMessages\(\{ force: true, authoritative: true \}\)/,
    'a stale cached card should replace itself with authoritative settled state',
  )
  const reconciliation = silentSubmit.slice(
    silentSubmit.indexOf('if (isQuestionStateChangedError(err))'),
    silentSubmit.indexOf('// QuestionCard owns this transient failure notice'),
  )
  assert.doesNotMatch(
    reconciliation,
    /setLiveQuestionId\(null\)/,
    'a failed authoritative refresh must leave the stale card answerable for retry',
  )
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
  const freeze = silentSubmit.indexOf('freezeQuestionSubmission(questionSubmissionContext)')
  const send = silentSubmit.indexOf('const response = await streamSend')

  assert.ok(freeze >= 0 && send > freeze,
    'the reader anchor must freeze synchronously before answer delivery resumes output')
  assert.match(silentSubmit, /if \(sendSilentInFlightRef\.current\) \{[\s\S]*?cancelPreparedQuestion\(\)/,
    'a rejected competing answer must retire its provisional press hold')
  assert.match(component, /useEffect\(\(\) => \(\) => cancelPreparedSubmission\(\)/,
    'an unmounted or replaced question card must retire an unclicked press')
  assert.match(component, /if \(selectionLocked \|\| disabled \|\| submitting\) cancelPreparedSubmission\(\)/,
    'a card made inactive before click must retire its provisional hold')
  assert.match(component, /if \(accepted === false \|\| accepted\?\.status === 'locally_queued' \|\| accepted\?\.status === 'locally_settled'\) \{[\s\S]*?onCancelAnswer\?\.\(preparedSubmission\)/,
    'a non-accepted answer must restore the activation-time base')
})

test('a pending question exposes Stop instead of an impossible steer', () => {
  assert.match(
    chatView,
    /const showSteer = !hasPendingQuestion[\s\S]*?const canSteer = canRequestSteer[\s\S]*?canFastForwardQueue/,
    'the composer must fall back to Stop while request_user_input owns the turn',
  )
  assert.match(
    chatView,
    /steerActive=\{turnActive && !hasPendingQuestion && deliveryReady\}/,
    'queued rows must not offer per-row steer while the live question blocks it',
  )
})


test('question submission labels distinguish local delivery from a committed answer', () => {
  const local = { chatId: 'labels', body: { question_id: 'labels-q', answers: { 'Next?': 'Yes' } } }
  for (const [records, answeredMap, label, platformAction] of [
    [[], undefined, 'Submit'],
    [[local], undefined, 'Queued on this device'],
    [[{ ...local, deliveryOutcome: 'delivered' }], undefined, 'Confirming answer…'],
    [[local], { 'Next?': 'Yes' }, 'Submitted'],
    [[], undefined, 'Continue', { type: 'restart', version: 2, status: 'pending', restart_option_id: 'yes' }],
  ]) {
    const html = renderToStaticMarkup(createElement(LocalAnswersContext.Provider, { value: records },
      createElement(QuestionCard, {
        chatId: 'labels', questionId: 'labels-q', answeredMap, platformAction,
        questions: [{ id: 'next', question: 'Next?', options: [{ id: 'yes', label: 'Yes' }] }],
      })))
    assert.match(html, new RegExp(`>${label}</button>`))
  }
})


test('terminal restart actions show upstream status detail without dead submission controls', () => {
  for (const [status, label] of [['restart_requested', 'Restart requested'], ['activated', 'Möbius restarted'], ['responded', 'Response sent']]) {
    const html = renderToStaticMarkup(createElement(QuestionCard, {
      chatId: 'restart-labels', questionId: 'restart-q',
      platformAction: { type: 'restart', version: 2, status, restart_option_id: 'restart' },
      questions: [{ id: 'next', question: 'Restart?', options: [{ id: 'restart', label: 'Restart now' }] }],
    }))
    assert.ok(html.includes(label))
    assert.match(html, /qcard__action-detail/)
    assert.doesNotMatch(html, /qcard__submit|qcard__opts/)
  }
})
