import { LocalAnswersContext } from './localAnswersContext.js'
import { useCallback, useContext, useEffect, useLayoutEffect, useRef, useState } from 'react'
import './QuestionCard.css'
import {
  clearQuestionDraft,
  questionDraftKey,
  readQuestionDraft,
  writeQuestionDraft,
} from './questionDraft.js'
import { autoGrowTextarea, textareaUsesNativeSizing } from './composerTextareaSizing.js'
import { placeCaretAtTextEnd } from './composerFocusPolicy.js'
import { isInlineEditorSubmit, isPlainTextPasteShortcut } from './composerShortcuts.js'
import {
  assistantClipboardText,
  insertClipboardText,
  queueClipboardTextUndoably,
} from './markdownClipboard.js'
import { isTouchPrimary } from '../../lib/pointerPrimary.js'
import {
  pointerSelectionChangedWithin,
  textSelectionSnapshot,
} from '../../lib/selectableTextControl.js'
import { getOnlineSnapshot } from '../../lib/connectivityStore.js'
import useFileUpload from './useFileUpload.js'
import FileChips from './FileChips.jsx'
import Attachments from './Attachments.jsx'
import { pastedFiles, filePasteNeedsDefaultPrevented } from './pasteUpload.js'
import { Paperclip } from '@openai/apps-sdk-ui/components/Icon'
import { fileBelongsToQuestion, resolveQuestionAnswer, questionAnswersReady, questionOptionSubmission } from './questionSubmission.js'
import {
  isRestartCardAction,
  restartCardSelectedOptions,
  restartCardStatusDetail,
  restartCardStatusLabel,
} from './restartCard.js'


const CUSTOM_ANSWER_MAX_HEIGHT = 180
// The server's bound on one answer's files; checked here so the owner hears
// it when attaching rather than as a failed submit.
const MAX_ANSWER_FILES = 20


function resizeCustomAnswer(textarea) {
  autoGrowTextarea(textarea, CUSTOM_ANSWER_MAX_HEIGHT)
}


export function CustomAnswerArea({
  answered,
  canSubmit,
  disabled,
  placeholder,
  onChange,
  onSubmitShortcut,
  onPasteFiles,
  question,
  value,
}) {
  const textareaRef = useRef(null)
  const plainPasteRef = useRef(false)
  const pendingCaretRef = useRef(null)

  useLayoutEffect(() => {
    const textarea = textareaRef.current
    resizeCustomAnswer(textarea)
    const caret = pendingCaretRef.current
    pendingCaretRef.current = null
    if (caret !== null) {
      try { textarea?.setSelectionRange(caret, caret) } catch { /* detached */ }
    }
  }, [value])

  // Paste behaves as in the message composer: files attach, copied Möbius
  // text keeps its Markdown (plain with Cmd/Ctrl+Shift+V), and the insertion
  // stays undoable where the browser allows it.
  function handlePaste(e) {
    const preferPlainText = plainPasteRef.current
    plainPasteRef.current = false
    if (answered) return
    const files = onPasteFiles ? pastedFiles(e.clipboardData) : []
    if (files.length) {
      if (filePasteNeedsDefaultPrevented(e.clipboardData, files)) e.preventDefault()
      onPasteFiles(files)
      return
    }
    const text = assistantClipboardText(e.clipboardData, preferPlainText)
    if (text === null) return
    e.preventDefault()
    const { selectionStart, selectionEnd } = e.currentTarget
    const insertControlled = () => {
      const next = insertClipboardText(value, selectionStart, selectionEnd, text)
      pendingCaretRef.current = next.caret
      onChange(next.value)
    }
    if (!queueClipboardTextUndoably(e.currentTarget, text, insertControlled)) insertControlled()
  }

  // The measured fallback also reacts to width: wrapping can add lines without
  // changing the answer value when a pane or device rotates.
  useEffect(() => {
    const textarea = textareaRef.current
    if (
      !textarea
      || textareaUsesNativeSizing()
      || typeof ResizeObserver === 'undefined'
    ) return undefined
    let lastWidth = -1
    const observer = new ResizeObserver(() => {
      const width = textarea.clientWidth
      if (width === lastWidth) return
      lastWidth = width
      resizeCustomAnswer(textarea)
    })
    observer.observe(textarea)
    return () => observer.disconnect()
  }, [])

  return (
    <textarea
      ref={textareaRef}
      className="qcard__input"
      data-chat-scroll-region
      data-chat-inline-editor="question-answer"
      aria-label={`Custom answer for: ${question}`}
      placeholder={answered ? 'No custom answer' : (placeholder || 'Or type your own answer…')}
      autoComplete="off"
      rows={1}
      wrap="soft"
      value={value}
      onChange={e => onChange(e.target.value)}
      onPaste={handlePaste}
      onKeyUp={() => { plainPasteRef.current = false }}
      onFocus={e => placeCaretAtTextEnd(e.currentTarget)}
      readOnly={answered}
      disabled={disabled && !answered}
      onKeyDown={e => {
        plainPasteRef.current = isPlainTextPasteShortcut(e)
        // Let Enter stay a newline until every question is answered, so a
        // half-filled grouped card can still take multi-line custom text.
        if (!canSubmit) return
        if (isInlineEditorSubmit(e, { isTouchPrimary: isTouchPrimary() })) {
          e.preventDefault()
          onSubmitShortcut(e.currentTarget.closest('.qcard'))
        }
      }}
    />
  )
}


export default function QuestionCard({
  chatId,
  questions,
  questionId,
  answeredMap,
  platformAction,
  submittedOptions,
  attachments,
  onAnswer,
  onPrepareAnswer,
  onCancelAnswer,
  disabled,
  // Callback ref that publishes this card's node to the "Möbius asked you
  // something — tap to answer" offscreen observer. Set only by the surface
  // rendering the answerable tail question, and only while the card is still
  // unanswered — the cue exists to send the owner back to a card that is
  // blocking the turn, and a submitted card no longer is. Because a live→
  // durable surface handoff remounts this component, the observer's target
  // has to come from here (the node's own render) rather than a lookup.
  pendingCardRef,
}) {
  const draftKey = questionDraftKey(chatId, questionId, questions)
  const [answers, setAnswers] = useState(
    () => readQuestionDraft(draftKey).answers,
  )
  const [otherTexts, setOtherTexts] = useState(
    () => readQuestionDraft(draftKey).otherTexts,
  )
  const [submitting, setSubmitting] = useState(false)
  const [submitted, setSubmitted] = useState(null)
  const [submitError, setSubmitError] = useState('')
  const pointerSelectionRef = useRef(null)
  const preparedSubmissionRef = useRef(null)
  const fileInputRef = useRef(null)
  const initialFilesRef = useRef(null)
  // Tagged files belong to their answer. Older untagged draft files were
  // card-level; the old paperclip's position did not establish ownership.
  if (initialFilesRef.current === null) {
    initialFilesRef.current = readQuestionDraft(draftKey).files
  }
  const attachTargetRef = useRef(null)
  const { files, addFiles, removeFile, clearFiles, discardFiles } = useFileUpload({ chatId, initialFiles: initialFilesRef.current })
  const readyFiles = files.filter(file => file.status === 'done')
  // The limit covers the whole card, matching the server's per-submission cap.
  function addAnswerFiles(list, question) {
    const room = MAX_ANSWER_FILES - files.length
    if (list.length > room) setSubmitError(`Attach at most ${MAX_ANSWER_FILES} files to one card.`)
    return room > 0 ? addFiles(list.slice(0, room), question) : Promise.resolve()
  }
  // Like the composer: wait for uploads in flight; a failed one shows its
  // error on the chip and is simply not sent.
  const pendingFiles = files.some(file => file.status === 'uploading')

  const localAnswers = useContext(LocalAnswersContext)
  const localAnswer = (localAnswers || []).find(record => (
    String(record.chatId) === String(chatId)
    && record.body?.question_id === questionId
  ))
  const actionStatusLabel = restartCardStatusLabel(platformAction)
  const actionStatusDetail = restartCardStatusDetail(platformAction)
  // A ready-boot receipt is independent of the offered action. Keep an
  // unanswered button and written reply available without manufacturing an
  // owner answer or bypassing the server's continuation hold.
  const completedAction = Boolean(actionStatusLabel)
    && platformAction?.status !== 'awaiting_owner'
  const answered = Boolean(submitted) || !!answeredMap || completedAction
  const locallyQueued = !answered && Boolean(localAnswer)
  const selectionLocked = answered || locallyQueued
  const attachLocked = selectionLocked || submitting || disabled
  const displayAnswers = answeredMap || localAnswer?.body?.answers || submitted?.answers || {}
  const grouped = questions.length > 1
  const restartAction = isRestartCardAction(platformAction)
  const writtenRestartAction = restartAction && platformAction?.version === 2
  const respondedRestartAction = (
    writtenRestartAction && platformAction?.status === 'responded'
  )

  // ChatView is keyed by chat, so switching away remounts this card. Keep an
  // unsubmitted selection in the same per-tab cache as composer drafts; the
  // owner can inspect another chat and return without rebuilding their answer.
  // Only a committed answer clears its draft. `disabled` is intentionally NOT
  // a clearing signal: during an offline reconnect the live/persisted render
  // sources can briefly hand off through a disabled card. Clearing there used
  // to erase the owner's selection precisely when the network returned.
  useEffect(() => {
    if (answered) {
      clearQuestionDraft(draftKey)
      // Files still held here were never sent from this tab. The server keeps
      // any another tab's answer claimed, so discarding them all is safe.
      if (files.length) discardFiles()
      return
    }
    writeQuestionDraft(draftKey, { answers, otherTexts, files })
  }, [draftKey, answers, otherTexts, files, answered, discardFiles])

  const allAnswered = questionAnswersReady(questions, answers, otherTexts, readyFiles)
  const selectedOptions = restartCardSelectedOptions(
    platformAction,
    questions,
    answers,
  )
  const canSubmit = allAnswered && !pendingFiles && (!restartAction || selectedOptions !== null)

  function selectOption(question, label) {
    if (selectionLocked || disabled) return
    const q = questions.find(qq => qq.question === question)
    if (!q?.multiSelect) {
      setOtherTexts(prev => ({ ...prev, [question]: '' }))
    }
    setAnswers(prev => {
      if (q?.multiSelect) {
        const current = prev[question] || []
        const arr = Array.isArray(current) ? current : [current]
        const next = arr.includes(label)
          ? arr.filter(l => l !== label)
          : [...arr, label]
        return { ...prev, [question]: next }
      }
      return { ...prev, [question]: label }
    })
  }

  function setOtherText(question, text) {
    setOtherTexts(prev => ({ ...prev, [question]: text }))
    setAnswers(prev => {
      const q = questions.find(qq => qq.question === question)
      if (q?.multiSelect) {
        const current = prev[question] || []
        const arr = Array.isArray(current) ? current : [current]
        const withoutOther = arr.filter(label => label !== '__other__')
        return {
          ...prev,
          [question]: text.trim()
            ? [...withoutOther, '__other__']
            : withoutOther,
        }
      }
      return {
        ...prev,
        [question]: text.trim()
          ? '__other__'
          : (prev[question] === '__other__' ? '' : prev[question]),
      }
    })
  }

  const cancelPreparedSubmission = useCallback(() => {
    const prepared = preparedSubmissionRef.current
    preparedSubmissionRef.current = null
    if (prepared) onCancelAnswer?.(prepared)
  }, [onCancelAnswer])

  useEffect(() => () => cancelPreparedSubmission(), [cancelPreparedSubmission])

  useEffect(() => {
    // A remote answer, source replacement, or competing submission can make
    // the button inactive between press and click. Retire the provisional hold
    // immediately; there will be no answer handoff to release it later.
    if (selectionLocked || disabled || submitting) cancelPreparedSubmission()
  }, [selectionLocked, cancelPreparedSubmission, disabled, submitting])

  async function handleSubmit(questionCard = null, preparedSubmission = null) {
    if (!canSubmit || selectionLocked || disabled || submitting) {
      if (preparedSubmission) onCancelAnswer?.(preparedSubmission)
      return
    }
    preparedSubmissionRef.current = null
    const resolved = {}
    const lines = questions.map(q => {
      const own = readyFiles.filter(file => fileBelongsToQuestion(file.group, q.question, questions.length))
      const val = resolveQuestionAnswer(answers[q.question], otherTexts[q.question])
        || (own.length ? `Attached ${own.length} file${own.length === 1 ? '' : 's'}` : '')
      resolved[q.question] = val
      const filesLine = own.length ? `\n  Files: ${own.map(file => file.name).join(', ')}` : ''
      return `- ${q.question}: ${val.replace(/\n/g, '\n  ')}${filesLine}`
    })
    const answerAttachments = readyFiles.map(({ name, size, mime_type, group }) => ({
      name, size, mime_type, ...(group != null ? { question: group } : {}),
    }))
    setSubmitError('')
    setSubmitting(true)
    try {
      const accepted = await onAnswer?.(
        lines.join('\n'),
        resolved,
        questionId,
        { questionCard, preparedSubmission, attachments: answerAttachments, ...questionOptionSubmission(questions, answers) },
      )
      // Only settle (and therefore clear the durable per-tab draft) after the
      // answer endpoint confirms that the transcript write committed.
      if (accepted === false || accepted?.status === 'locally_queued' || accepted?.status === 'locally_settled') {
        if (preparedSubmission) onCancelAnswer?.(preparedSubmission)
      } else {
        setSubmitted({ answers: resolved, attachments: answerAttachments })
        clearFiles()
      }
    } catch (error) {
      // Keep the choices and custom text intact so a transient failure is
      // immediately retryable. Keep the notice on the card too: adding an
      // assistant-looking error row after it makes the question cease to be
      // the transcript tail and disables the very retry the owner needs.
      // Only a 409 explains the answer itself (why it cannot be accepted), so
      // show its detail; every other failure keeps the friendly retry copy so
      // raw backend strings never reach the card.
      const rejectionDetail = error?.status === 409 && typeof error?.detail === 'string'
        ? error.detail.trim()
        : ''
      setSubmitError(
        !getOnlineSnapshot()
          ? 'You’re offline. Your choice is saved — submit it when you’re back online.'
          : rejectionDetail
            || 'That answer didn’t save. Your choice is still here — please try again.',
      )
    } finally {
      setSubmitting(false)
    }
  }

  let submitLabel = writtenRestartAction ? 'Continue' : 'Submit'
  if (answered) submitLabel = 'Submitted'
  if (submitting) submitLabel = 'Submitting…'
  if (locallyQueued) submitLabel = localAnswer.deliveryOutcome === 'delivered'
    ? 'Confirming answer…' : 'Queued on this device'

  // Files sit inside their answer box. On grouped cards, older untagged files
  // stay in a shared lane; only a single-question card can own them unambiguously.
  const sentAttachments = attachments || localAnswer?.body?.attachments || submitted?.attachments || []
  const answerFiles = question => platformAction ? null : (
    <div className="qcard__answer-files" role="group" aria-label="Files for this answer">
      {selectionLocked
        ? <Attachments attachments={sentAttachments.filter(file => fileBelongsToQuestion(file.question, question, questions.length))} chatId={chatId} />
        : <FileChips files={files.filter(file => fileBelongsToQuestion(file.group, question, questions.length))} onRemove={removeFile} chatId={chatId} disabled={submitting || disabled} />}
    </div>
  )
  const sharedFiles = grouped
    ? (selectionLocked
      ? sentAttachments.filter(file => file.question == null)
      : files.filter(file => file.group == null))
    : []

  return (
    <div
      className={`qcard${grouped ? ' qcard--grouped' : ''}${answered ? ' qcard--answered' : ''}`}
      data-scroll-anchor-key={draftKey}
      ref={answered ? null : pendingCardRef}
      aria-disabled={disabled && !answered ? true : undefined}
      aria-label={grouped ? `${questions.length} decisions` : undefined}
    >
      {actionStatusLabel && (
        <div className="qcard__action-status" role="status">
          {actionStatusLabel}
          {actionStatusDetail && (
            <span className="qcard__action-detail">{actionStatusDetail}</span>
          )}
        </div>
      )}
      {grouped && (
        <div className="qcard__group-head">
          <div>
            <div className="qcard__group-title">{questions.length} decisions</div>
            <div className="qcard__group-copy">
              Answer each question, then submit them together.
            </div>
          </div>
          <span className="qcard__group-count">{questions.length}</span>
        </div>
      )}
      <div className="qcard__questions">
        {questions.map((q, qi) => {
        const selected = answers[q.question]
        const isMulti = q.multiSelect
        const options = q.options || []
        const visibleOptions = writtenRestartAction
          ? options.filter(opt => opt.id === platformAction.restart_option_id)
          : options
        const hasOptions = visibleOptions.length > 0
        const selectedArr = isMulti
          ? (Array.isArray(selected) ? selected : [])
          : []
        const isOtherSelected = isMulti
          ? selectedArr.includes('__other__')
          : selected === '__other__'
        const inactive = selectionLocked || disabled || submitting

        const answeredValue = displayAnswers[q.question]
          || (submitted ? resolveQuestionAnswer(answers[q.question], otherTexts[q.question]) : '')
        const answeredArr = selectionLocked && isMulti
          ? (answeredValue ? answeredValue.split(', ').map(s => s.trim()) : [])
          : []
        const unmatchedAnswers = selectionLocked
          ? (isMulti
              ? answeredArr.filter(v => !options.some(o => o.label === v))
              : (answeredValue && !options.some(o => o.label === answeredValue)
                  ? [answeredValue]
                  : []))
          : []
        const writtenRestartResponse = Boolean(
          respondedRestartAction
          && submittedOptions
          && Object.keys(submittedOptions).length === 0
        )
        const writtenAnswer = writtenRestartResponse || !hasOptions
          ? answeredValue
          : unmatchedAnswers.join(', ')
        const answeredWithOther = writtenAnswer.length > 0
        const selectionCount = selectionLocked
          ? (isMulti ? answeredArr.length : (answeredValue ? 1 : 0))
          : selectedArr.length

        return (
          <div key={qi} className="qcard__q">
            {q.header && (
              <div className="qcard__header">{q.header}</div>
            )}
            <div className="qcard__text">{q.question}</div>
            {/* Single- vs multi-select was indistinguishable until you tapped
                and watched whether a prior pick cleared. Surface it up front:
                a caption (with a live count for multi) plus a per-option glyph
                (□ checkbox for multi, ○ radio for single). */}
            {!completedAction && (!disabled || answered) && hasOptions && (
              <div className="qcard__hint">
                {writtenRestartAction
                  ? platformAction.observation?.observed_at
                    ? 'Restart again, or reply below'
                    : 'Restart now, or reply below'
                  : isMulti
                  ? `Select all that apply${selectionCount ? ` · ${selectionCount} selected` : ''}`
                  : 'Choose one'}
              </div>
            )}
            {/* Selection state was conveyed only by a CSS class — silent to
                screen readers. Expose it as a radiogroup (single) / group of
                checkboxes (multi) with per-option aria-checked. */}
            {!completedAction && hasOptions && <div
              className="qcard__opts"
              role={isMulti ? 'group' : 'radiogroup'}
              aria-label={q.question}
            >
              {/* For multi-select answered state, the comma-joined value is
                  parsed above so each chosen option highlights correctly. */}
              {visibleOptions.map((opt, oi) => {
                const isChosen = selectionLocked
                  ? (isMulti ? answeredArr.includes(opt.label) : answeredValue === opt.label)
                  : false
                const isActive = selectionLocked
                  ? isChosen
                  : (isMulti ? selectedArr.includes(opt.label) : selected === opt.label)
                const dimmed = answered && !isChosen
                const OptionSurface = inactive ? 'div' : 'button'
                return (
                  <OptionSurface
                    key={oi}
                    type={inactive ? undefined : 'button'}
                    role={isMulti ? 'checkbox' : 'radio'}
                    aria-checked={isActive}
                    aria-disabled={inactive || undefined}
                    className={`qcard__opt${isActive ? ' qcard__opt--on' : ''}${dimmed ? ' qcard__opt--dim' : ''}${inactive ? ' qcard__opt--static' : ''}`}
                    onPointerDown={inactive ? undefined : () => {
                      pointerSelectionRef.current = textSelectionSnapshot()
                    }}
                    onClick={inactive ? undefined : (event) => {
                      const selectionBeforePointer = pointerSelectionRef.current
                      pointerSelectionRef.current = null
                      if (
                        event.detail !== 0
                        && pointerSelectionChangedWithin(
                          selectionBeforePointer,
                          event.currentTarget,
                        )
                      ) return
                      selectOption(q.question, opt.label)
                    }}
                    title={opt.description || ''}
                  >
                    <span
                      className={`qcard__mark qcard__mark--${isMulti ? 'box' : 'radio'}`}
                      aria-hidden="true"
                    />
                    {/* Description renders inline, not only as title= — a
                        title tooltip is invisible on touch, and this is a
                        phone-first surface. */}
                    {opt.description ? (
                      <span className="qcard__opt-body">
                        <span className="qcard__opt-label">{opt.label}</span>
                        <span className="qcard__opt-desc">{opt.description}</span>
                      </span>
                    ) : (
                      opt.label
                    )}
                  </OptionSurface>
                )
              })}
            </div>}
            {(!completedAction || respondedRestartAction)
              && (!restartAction || writtenRestartAction) && (
              <div className="qcard__answer-row">
                {/* Each answer has its own paperclip on the left of its box.
                    It stays while the action row does, so Submit, queueing
                    and the Submitted state never move the card; once the
                    answer is locked it only stops taking files. */}
                {!platformAction && (answered || !disabled) && (
                  <button type="button" className="qcard__attach"
                    aria-label={grouped ? `Attach a photo or file to your answer to: ${q.question}` : 'Attach a photo or file'}
                    title="Attach a photo or file" disabled={attachLocked} onClick={() => {
                      attachTargetRef.current = q.question
                      fileInputRef.current?.click()
                    }}>
                    <Paperclip width={18} height={18} aria-hidden="true" />
                  </button>
                )}
                <div className={`qcard__composer${isOtherSelected || answeredWithOther ? ' qcard__composer--active' : ''}`}>
                  {answerFiles(q.question)}
                  <CustomAnswerArea
                    answered={selectionLocked}
                    canSubmit={canSubmit}
                    disabled={inactive}
                    placeholder={writtenRestartAction
                      ? 'Or tell me what you’d like to do instead…'
                      : hasOptions ? undefined : 'Type your answer…'}
                    onChange={text => setOtherText(q.question, text)}
                    onPasteFiles={platformAction || inactive ? undefined : pasted => addAnswerFiles(pasted, q.question)}
                    onSubmitShortcut={(questionCard) => {
                      if (canSubmit) handleSubmit(questionCard, null)
                    }}
                    question={q.question}
                    value={selectionLocked
                      ? writtenAnswer
                      : (otherTexts[q.question] || '')}
                  />
                </div>
              </div>
            )}
          </div>
        )
        })}
      </div>
      {!platformAction && sharedFiles.length > 0 && (
        <div className="qcard__shared-files" role="group" aria-label="Shared card files">
          <div className="qcard__shared-files-label">Shared files · not assigned to a question</div>
          {selectionLocked
            ? <Attachments attachments={sharedFiles} chatId={chatId} />
            : <FileChips files={sharedFiles} onRemove={removeFile} chatId={chatId} disabled={submitting || disabled} />}
        </div>
      )}
      {!platformAction && (answered || !disabled) && (
        <input ref={fileInputRef} type="file" multiple className="qcard__file-input"
          disabled={attachLocked}
          aria-label="Attach files to your answer"
          onChange={e => {
            const selected = Array.from(e.target.files || [])
            e.target.value = ''
            if (!attachLocked && attachTargetRef.current) addAnswerFiles(selected, attachTargetRef.current)
          }} />
      )}
      {!completedAction && (answered || !disabled) && (
        <>
          {locallyQueued && (
            <div className="qcard__queue-status" role="status">
              {localAnswer.deliveryOutcome === 'delivered'
                ? 'Your answer reached Möbius. Confirming this card…'
                : 'Your answer is saved here and will send when Möbius reconnects.'}
            </div>
          )}
          {submitError && !answered && !locallyQueued && (
            <div className="qcard__submit-error" role="status">{submitError}</div>
          )}
          <button
            type="button"
            className="qcard__submit"
            onPointerDown={(event) => {
              if (event.button !== 0 || event.isPrimary === false) return
              const questionCard = event.currentTarget.closest('.qcard')
              preparedSubmissionRef.current = onPrepareAnswer?.(questionCard) || null
            }}
            onPointerCancel={cancelPreparedSubmission}
            onBlur={cancelPreparedSubmission}
            onPointerLeave={(event) => {
              if (event.buttons !== 0) cancelPreparedSubmission()
            }}
            onClick={(event) => {
              const prepared = preparedSubmissionRef.current
              preparedSubmissionRef.current = null
              handleSubmit(event.currentTarget.closest('.qcard'), prepared)
            }}
            disabled={!canSubmit || disabled || selectionLocked || submitting}
          >
            {submitLabel}
          </button>
        </>
      )}
    </div>
  )
}
