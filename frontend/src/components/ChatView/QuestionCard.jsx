import { useEffect, useState } from 'react'
import './QuestionCard.css'
import {
  clearQuestionDraft,
  questionDraftKey,
  readQuestionDraft,
  writeQuestionDraft,
} from './questionDraft.js'


function resolveAnswer(answer, otherText) {
  if (Array.isArray(answer)) {
    return answer.map(v => v === '__other__' ? otherText?.trim() || '' : v)
      .filter(Boolean).join(', ')
  }
  if (answer === '__other__') return otherText?.trim() || ''
  return answer || ''
}


export default function QuestionCard({
  chatId,
  questions,
  questionId,
  answeredMap,
  onAnswer,
  disabled,
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

  const answered = submitted || !!answeredMap
  const displayAnswers = answeredMap || {}

  // ChatView is keyed by chat, so switching away remounts this card. Keep an
  // unsubmitted selection in the same per-tab cache as composer drafts; the
  // owner can inspect another chat and return without rebuilding their answer.
  // A completed or superseded card clears its draft instead of leaving stale
  // choices attached to transcript history.
  useEffect(() => {
    if (answered || disabled) {
      clearQuestionDraft(draftKey)
      return
    }
    writeQuestionDraft(draftKey, answers, otherTexts)
  }, [draftKey, answers, otherTexts, answered, disabled])

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
    setAnswers(prev => {
      const q = questions.find(qq => qq.question === question)
      if (q?.multiSelect) {
        const current = prev[question] || []
        const arr = Array.isArray(current) ? current : [current]
        const next = arr.includes(label)
          ? arr.filter(l => l !== label)
          : [...arr.filter(l => l !== '__other__'), label]
        return { ...prev, [question]: next }
      }
      return { ...prev, [question]: label }
    })
  }

  function setOtherText(question, text) {
    setOtherTexts(prev => ({ ...prev, [question]: text }))
  }

  function selectOther(question) {
    if (answered || disabled) return
    const q = questions.find(qq => qq.question === question)
    if (q?.multiSelect) {
      setAnswers(prev => {
        const current = prev[question] || []
        const arr = Array.isArray(current) ? current : [current]
        if (arr.includes('__other__')) {
          return { ...prev, [question]: arr.filter(l => l !== '__other__') }
        }
        return { ...prev, [question]: [...arr, '__other__'] }
      })
    } else {
      setAnswers(prev => ({ ...prev, [question]: '__other__' }))
    }
  }

  async function handleSubmit() {
    if (!allAnswered || answered || disabled || submitting) return
    const resolved = {}
    const lines = questions.map(q => {
      const val = resolveAnswer(answers[q.question], otherTexts[q.question])
      resolved[q.question] = val
      return `- ${q.question}: ${val}`
    })
    setSubmitting(true)
    try {
      const accepted = await onAnswer?.(lines.join('\n'), resolved, questionId)
      // Only settle (and therefore clear the durable per-tab draft) after the
      // answer endpoint confirms that the transcript write committed.
      if (accepted !== false) setSubmitted(true)
    } catch {
      // ChatView presents the transport/stale error. Keep the choices and
      // custom text intact so a transient failure is immediately retryable.
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div
      className={`qcard${answered ? ' qcard--answered' : ''}`}
      aria-disabled={disabled && !answered ? true : undefined}
    >
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
                  return (
                    <button
                      key={oi}
                      type="button"
                      role={isMulti ? 'checkbox' : 'radio'}
                      aria-checked={isActive}
                      className={`qcard__opt${isActive ? ' qcard__opt--on' : ''}${dimmed ? ' qcard__opt--dim' : ''}`}
                      onClick={answered ? undefined : () => selectOption(q.question, opt.label)}
                      disabled={inactive}
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
                    </button>
                  )
                })
              })()}
              <button
                type="button"
                role={isMulti ? 'checkbox' : 'radio'}
                aria-checked={answered ? answeredWithOther : isOtherSelected}
                className={`qcard__opt qcard__opt--other${(answered ? answeredWithOther : isOtherSelected) ? ' qcard__opt--on' : ''}${answered && !answeredWithOther ? ' qcard__opt--dim' : ''}`}
                onClick={answered ? undefined : () => selectOther(q.question)}
                disabled={inactive}
              >
                <span
                  className={`qcard__mark qcard__mark--${isMulti ? 'box' : 'radio'}`}
                  aria-hidden="true"
                />
                Other
              </button>
            </div>
            {(isOtherSelected || answeredWithOther) && (
              <input
                className="qcard__input"
                type="text"
                aria-label={`Other answer for: ${q.question}`}
                placeholder="Type your answer…"
                autoComplete="off"
                value={answered
                  ? unmatchedAnswers.join(', ')
                  : (otherTexts[q.question] || '')}
                onChange={e => setOtherText(q.question, e.target.value)}
                disabled={inactive}
                autoFocus
                onKeyDown={e => {
                  if (e.key === 'Enter' && allAnswered) {
                    e.preventDefault()
                    handleSubmit()
                  }
                }}
              />
            )}
          </div>
        )
      })}
      {(answered || !disabled) && (
        <button
          type="button"
          className="qcard__submit"
          onClick={handleSubmit}
          disabled={!allAnswered || disabled || answered || submitting}
        >
          {submitting ? 'Submitting…' : (answered ? 'Submitted' : 'Submit')}
        </button>
      )}
    </div>
  )
}
