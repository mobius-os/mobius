import { useState } from 'react'
import './QuestionCard.css'


function resolveAnswer(answer, otherText) {
  if (Array.isArray(answer)) {
    return answer.map(v => v === '__other__' ? otherText?.trim() || '' : v)
      .filter(Boolean).join(', ')
  }
  if (answer === '__other__') return otherText?.trim() || ''
  return answer || ''
}


export default function QuestionCard({ questions, questionId, answeredMap, onAnswer, disabled }) {
  const [answers, setAnswers] = useState({})
  const [otherTexts, setOtherTexts] = useState({})
  const [submitted, setSubmitted] = useState(false)

  const answered = submitted || !!answeredMap
  const displayAnswers = answeredMap || {}

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

  function handleSubmit() {
    if (!allAnswered || answered || disabled) return
    const resolved = {}
    const lines = questions.map(q => {
      const val = resolveAnswer(answers[q.question], otherTexts[q.question])
      resolved[q.question] = val
      return `- ${q.question}: ${val}`
    })
    setSubmitted(true)
    onAnswer?.(lines.join('\n'), resolved, questionId)
  }

  return (
    <div className={`qcard${answered ? ' qcard--answered' : ''}`}>
      {questions.map((q, qi) => {
        const selected = answers[q.question]
        const isMulti = q.multiSelect
        const selectedArr = isMulti
          ? (Array.isArray(selected) ? selected : [])
          : []
        const isOtherSelected = isMulti
          ? selectedArr.includes('__other__')
          : selected === '__other__'
        const inactive = answered || disabled

        const answeredValue = displayAnswers[q.question]
          || (submitted ? resolveAnswer(answers[q.question], otherTexts[q.question]) : '')

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
            {!answered && (
              <div className="qcard__hint">
                {isMulti
                  ? `Select all that apply${selectedArr.length ? ` · ${selectedArr.length} selected` : ''}`
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
              {/* For multi-select answered state, parse the comma-joined
                  value back into an array so each chosen option highlights
                  correctly. Without this split, isChosen = (fullString ===
                  opt.label) was always false and a fallback span rendered
                  the whole joined string instead of individual chips. */}
              {(() => {
                const answeredArr = answered && isMulti
                  ? (answeredValue ? answeredValue.split(', ').map(s => s.trim()) : [])
                  : null

                return q.options?.map((opt, oi) => {
                  const isChosen = answered
                    ? (isMulti ? (answeredArr?.includes(opt.label) ?? false) : answeredValue === opt.label)
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
              {!answered && (
              <button
                type="button"
                role={isMulti ? 'checkbox' : 'radio'}
                aria-checked={isOtherSelected}
                className={`qcard__opt qcard__opt--other${isOtherSelected ? ' qcard__opt--on' : ''}`}
                onClick={() => selectOther(q.question)}
                disabled={inactive}
              >
                <span
                  className={`qcard__mark qcard__mark--${isMulti ? 'box' : 'radio'}`}
                  aria-hidden="true"
                />
                Other
              </button>
              )}
              {/* Fallback span for "other" text answers that don't match
                  any known option label. For multi-select, show the unmatched
                  entries; for single-select, show the whole value. */}
              {answered && isMulti && (() => {
                const answeredArr = answeredValue ? answeredValue.split(', ').map(s => s.trim()) : []
                const unmatched = answeredArr.filter(v => !q.options?.some(o => o.label === v))
                if (unmatched.length === 0) return null
                return <span className="qcard__opt qcard__opt--on">{unmatched.join(', ')}</span>
              })()}
              {answered && !isMulti && answeredValue && !q.options?.some(o => o.label === answeredValue) && (
                <span className="qcard__opt qcard__opt--on">{answeredValue}</span>
              )}
            </div>
            {isOtherSelected && !answered && (
              <input
                className="qcard__input"
                type="text"
                placeholder="Type your answer..."
                value={otherTexts[q.question] || ''}
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
      {!answered && (
        <button
          type="button"
          className="qcard__submit"
          onClick={handleSubmit}
          disabled={!allAnswered || disabled}
        >
          Submit
        </button>
      )}
    </div>
  )
}
