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
import { isInlineEditorSubmit } from './composerShortcuts.js'
import { isTouchPrimary } from '../../lib/pointerPrimary.js'
import {
  pointerSelectionChangedWithin,
  textSelectionSnapshot,
} from '../../lib/selectableTextControl.js'
import { getOnlineSnapshot } from '../../lib/connectivityStore.js'
import useFileUpload from './useFileUpload.js'
import { FileChips } from './ChatInputBar.jsx'
import Attachments from './Attachments.jsx'
import { pastedFiles, filePasteNeedsDefaultPrevented } from './pasteUpload.js'
import { Paperclip } from '@openai/apps-sdk-ui/components/Icon'
import { questionOptionSubmission } from './questionSubmission.js'
import {
  isRestartCardAction,
  restartCardSelectedOptions,
  restartCardStatusDetail,
  restartCardStatusLabel,
} from './restartCard.js'


function resolveAnswer(answer, otherText) {
  if (Array.isArray(answer)) {
    return answer.map(v => v === '__other__' ? otherText?.trim() || '' : v)
      .filter(Boolean).join(', ')
  }
  if (answer === '__other__') return otherText?.trim() || ''
  return answer || ''
}


const CUSTOM_ANSWER_MAX_HEIGHT = 180


function resizeCustomAnswer(textarea) {
  autoGrowTextarea(textarea, CUSTOM_ANSWER_MAX_HEIGHT)
}


function CustomAnswerArea({
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

  useLayoutEffect(() => {
    resizeCustomAnswer(textareaRef.current)
  }, [value])

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
      onPaste={e => {
        if (!onPasteFiles) return
        const files = pastedFiles(e.clipboardData)
        if (!files.length) return
        if (filePasteNeedsDefaultPrevented(e.clipboardData, files)) e.preventDefault()
        onPasteFiles?.(files)
      }}
      onFocus={e => placeCaretAtTextEnd(e.currentTarget)}
      readOnly={answered}
      disabled={disabled && !answered}
      onKeyDown={e => {
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
  const [submitted, setSubmitted] = useState(false)
  const [submitError, setSubmitError] = useState('')
  const pointerSelectionRef = useRef(null)
  const preparedSubmissionRef = useRef(null)
  const fileInputRef = useRef(null)
  const initialFilesRef = useRef(null)
  if (initialFilesRef.current === null) initialFilesRef.current = readQuestionDraft(draftKey).files
  const { files, addFiles, removeFile, clearFiles } = useFileUpload({ chatId, initialFiles: initialFilesRef.current })
  const readyFiles = files.filter(file => file.status === 'done')
  const uploadingFiles = files.some(file => file.status === 'uploading')

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
  const answered = submitted || !!answeredMap || completedAction
  const locallyQueued = !answered && Boolean(localAnswer)
  const selectionLocked = answered || locallyQueued
  const displayAnswers = answeredMap || localAnswer?.body?.answers || {}
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
      return
    }
    writeQuestionDraft(draftKey, answers, otherTexts, undefined, files)
  }, [draftKey, answers, otherTexts, files, answered])

  const allAnswered = questions.every(q => {
    const a = answers[q.question]
    if (!a) return q === questions[0] && readyFiles.length > 0
    if (Array.isArray(a)) {
      if (a.length === 0) return false
      if (a.includes('__other__') && !otherTexts[q.question]?.trim()) return false
      return true
    }
    if (a === '__other__') return !!otherTexts[q.question]?.trim() || (q === questions[0] && readyFiles.length > 0)
    return true
  })
  const selectedOptions = restartCardSelectedOptions(
    platformAction,
    questions,
    answers,
  )
  const canSubmit = allAnswered && !uploadingFiles && (!restartAction || selectedOptions !== null)

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
      const val = resolveAnswer(answers[q.question], otherTexts[q.question])
        || (q === questions[0] && readyFiles.length ? `Attached ${readyFiles.length} file${readyFiles.length === 1 ? '' : 's'}` : '')
      resolved[q.question] = val
      return `- ${q.question}: ${val.replace(/\n/g, '\n  ')}`
    })
    setSubmitError('')
    setSubmitting(true)
    try {
      const accepted = await onAnswer?.(
        lines.join('\n'),
        resolved,
        questionId,
        { questionCard, preparedSubmission, attachments: readyFiles.map(({ name, size, mime_type }) => ({ name, size, mime_type })), ...questionOptionSubmission(questions, answers) },
      )
      // Only settle (and therefore clear the durable per-tab draft) after the
      // answer endpoint confirms that the transcript write committed.
      if (accepted === false || accepted?.status === 'locally_queued' || accepted?.status === 'locally_settled') {
        if (preparedSubmission) onCancelAnswer?.(preparedSubmission)
      } else {
        setSubmitted(true)
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

  return (
    <div
      className={`qcard${grouped ? ' qcard--grouped' : ''}${answered ? ' qcard--answered' : ''}`}
      onDrop={event => {
        if (selectionLocked || disabled || platformAction) return
        const dropped = Array.from(event.dataTransfer?.files || [])
        if (!dropped.length) return
        event.preventDefault()
        addFiles(dropped)
      }}
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
          || (submitted ? resolveAnswer(answers[q.question], otherTexts[q.question]) : '')
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
                {qi === 0 && !selectionLocked && !disabled && !platformAction && (
                  <div className="qcard__composer-actions">
                    <input ref={fileInputRef} type="file" multiple className="qcard__file-input"
                      aria-label="Attach files to your answer"
                      onChange={e => { const selected = Array.from(e.target.files || []); e.target.value = ''; addFiles(selected) }} />
                    <button type="button" className="qcard__attach" aria-label="Attach a photo or file"
                      title="Attach a photo or file" onClick={() => fileInputRef.current?.click()}>
                      <Paperclip width={18} height={18} aria-hidden="true" />
                    </button>
                  </div>
                )}
                <div className={`qcard__composer${isOtherSelected || answeredWithOther ? ' qcard__composer--active' : ''}`}>
                  {qi === 0 && (selectionLocked
                    ? <Attachments attachments={attachments || localAnswer?.body?.attachments} chatId={chatId} />
                    : files.length > 0 && <FileChips files={files} onRemove={removeFile} chatId={chatId} />)}
                  <CustomAnswerArea
                    answered={selectionLocked}
                    canSubmit={allAnswered}
                    disabled={inactive}
                    placeholder={writtenRestartAction
                      ? 'Or tell me what you’d like to do instead…'
                      : hasOptions ? undefined : 'Type your answer…'}
                    onChange={text => setOtherText(q.question, text)}
                    onPasteFiles={platformAction || selectionLocked || disabled ? undefined : addFiles}
                    onSubmitShortcut={(questionCard) => {
                      if (allAnswered) handleSubmit(questionCard, null)
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
