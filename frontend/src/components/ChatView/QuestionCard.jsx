import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import './QuestionCard.css'
import {
  clearQuestionDraft,
  questionDraftKey,
  readQuestionDraft,
  writeQuestionDraft,
} from './questionDraft.js'
import { textareaUsesNativeSizing } from './composerTextareaSizing.js'
import {
  pointerSelectionChangedWithin,
  textSelectionSnapshot,
} from '../../lib/selectableTextControl.js'


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
  if (!textarea || textareaUsesNativeSizing()) return
  textarea.style.height = 'auto'
  const contentHeight = textarea.scrollHeight
  textarea.style.height = `${Math.min(contentHeight, CUSTOM_ANSWER_MAX_HEIGHT)}px`
  textarea.style.overflowY = contentHeight > CUSTOM_ANSWER_MAX_HEIGHT
    ? 'auto'
    : 'hidden'
}


function CustomAnswerArea({
  active,
  answered,
  disabled,
  onChange,
  onSubmitShortcut,
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
      className={`qcard__input${active ? ' qcard__input--active' : ''}`}
      aria-label={`Custom answer for: ${question}`}
      placeholder={answered ? 'No custom answer' : 'Or type your own answer…'}
      autoComplete="off"
      rows={1}
      value={value}
      onChange={e => onChange(e.target.value)}
      readOnly={answered}
      disabled={disabled && !answered}
      onKeyDown={e => {
        if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
          e.preventDefault()
          onSubmitShortcut()
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
  onAnswer,
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

  const answered = submitted || !!answeredMap
  const displayAnswers = answeredMap || {}
  const grouped = questions.length > 1

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
    writeQuestionDraft(draftKey, answers, otherTexts)
  }, [draftKey, answers, otherTexts, answered])

  const allAnswered = questions.every(q => {
    const a = answers[q.question]
    if (!a) return false
    if (Array.isArray(a)) {
      if (a.length === 0) return false
      if (a.includes('__other__') && !otherTexts[q.question]?.trim()) return false
      return true
    }
    if (a === '__other__') return !!otherTexts[q.question]?.trim()
    return true
  })

  function selectOption(question, label) {
    if (answered || disabled) return
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

  async function handleSubmit() {
    if (!allAnswered || answered || disabled || submitting) return
    const resolved = {}
    const lines = questions.map(q => {
      const val = resolveAnswer(answers[q.question], otherTexts[q.question])
      resolved[q.question] = val
      return `- ${q.question}: ${val.replace(/\n/g, '\n  ')}`
    })
    setSubmitError('')
    setSubmitting(true)
    try {
      const accepted = await onAnswer?.(lines.join('\n'), resolved, questionId)
      // Only settle (and therefore clear the durable per-tab draft) after the
      // answer endpoint confirms that the transcript write committed.
      if (accepted !== false) setSubmitted(true)
    } catch {
      // Keep the choices and custom text intact so a transient failure is
      // immediately retryable. Keep the notice on the card too: adding an
      // assistant-looking error row after it makes the question cease to be
      // the transcript tail and disables the very retry the owner needs.
      setSubmitError(
        typeof navigator !== 'undefined' && navigator.onLine === false
          ? 'You’re offline. Your choice is saved — submit it when you’re back online.'
          : 'That answer didn’t save. Your choice is still here — please try again.',
      )
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className={`qcard${grouped ? ' qcard--grouped' : ''}${answered ? ' qcard--answered' : ''}`}
      ref={answered ? null : pendingCardRef}
      aria-disabled={disabled && !answered ? true : undefined}
      aria-label={grouped ? `${questions.length} decisions` : undefined}
    >
      {grouped && (
        <div className="qcard__group-head">
          <div>
            <div className="qcard__group-title">{questions.length} decisions</div>
            <div className="qcard__group-copy">
              Choose each one, then submit them together.
            </div>
          </div>
          <span className="qcard__group-count">{questions.length}</span>
        </div>
      )}
      <div className="qcard__questions">
        {questions.map((q, qi) => {
        const selected = answers[q.question]
        const isMulti = q.multiSelect
        const selectedArr = isMulti
          ? (Array.isArray(selected) ? selected : [])
          : []
        const isOtherSelected = isMulti
          ? selectedArr.includes('__other__')
          : selected === '__other__'
        const inactive = answered || disabled || submitting

        const answeredValue = displayAnswers[q.question]
          || (submitted ? resolveAnswer(answers[q.question], otherTexts[q.question]) : '')
        const answeredArr = answered && isMulti
          ? (answeredValue ? answeredValue.split(', ').map(s => s.trim()) : [])
          : []
        const unmatchedAnswers = answered
          ? (isMulti
              ? answeredArr.filter(v => !q.options?.some(o => o.label === v))
              : (answeredValue && !q.options?.some(o => o.label === answeredValue)
                  ? [answeredValue]
                  : []))
          : []
        const answeredWithOther = unmatchedAnswers.length > 0
        const selectionCount = answered
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
            {(!disabled || answered) && (
              <div className="qcard__hint">
                {isMulti
                  ? `Select all that apply${selectionCount ? ` · ${selectionCount} selected` : ''}`
                  : 'Choose one'}
              </div>
            )}
            {/* Selection state was conveyed only by a CSS class — silent to
                screen readers. Expose it as a radiogroup (single) / group of
                checkboxes (multi) with per-option aria-checked. */}
            <div
              className="qcard__opts"
              role={isMulti ? 'group' : 'radiogroup'}
              aria-label={q.question}
            >
              {/* For multi-select answered state, the comma-joined value is
                  parsed above so each chosen option highlights correctly. */}
              {(() => {
                return q.options?.map((opt, oi) => {
                  const isChosen = answered
                    ? (isMulti ? answeredArr.includes(opt.label) : answeredValue === opt.label)
                    : false
                  const isActive = answered
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
                })
              })()}
            </div>
            <CustomAnswerArea
              active={isOtherSelected || answeredWithOther}
              answered={answered}
              disabled={inactive}
              onChange={text => setOtherText(q.question, text)}
              onSubmitShortcut={() => {
                if (allAnswered) handleSubmit()
              }}
              question={q.question}
              value={answered
                ? unmatchedAnswers.join(', ')
                : (otherTexts[q.question] || '')}
            />
          </div>
        )
        })}
      </div>
      {(answered || !disabled) && (
        <>
          {submitError && !answered && (
            <div className="qcard__submit-error" role="status">{submitError}</div>
          )}
          <button
            type="button"
            className="qcard__submit"
            onClick={handleSubmit}
            disabled={!allAnswered || disabled || answered || submitting}
          >
            {submitting ? 'Submitting…' : (answered ? 'Submitted' : 'Submit')}
          </button>
        </>
      )}
    </div>
  )
}
