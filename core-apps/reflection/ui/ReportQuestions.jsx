import { useEffect, useState } from 'react'

// Native tap-card UI for the brief's in-report questions. Mirrors the shell
// QuestionCard's shape ({question, header, multiSelect, options:[{label,
// description}]}) but is a single-file, install-safe copy — no sibling
// imports, no streaming/answeredMap plumbing. Collects an answer per question;
// on submit it persists { "<question text>": "<chosen label(s)>" } to
// question-answers/<date>.json for the NEXT run (not a live agent) and flips to
// "answered" only once durableWrite resolves a durable outcome (synced or
// queued); a fatal server refusal rejects, so it never claims "Saved" falsely.
// It also
// pre-seeds the answered state from the same record on open, so a reopened
// brief shows the done state rather than a fresh re-submittable form. Mirrors
// the News app's ReportQuestions (app-news/index.jsx) so the two apps read the same.
export function ReportQuestions({ questions, storage, dateStr, appId, token }) {
  const [picks, setPicks] = useState({})        // question INDEX -> label | [labels]
  const [answered, setAnswered] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  // `answered` cannot start false-then-flip-on-read: a fresh form briefly
  // visible before the pre-seed lands invites a duplicate submit. Gate the
  // form behind a one-time seed check so a reopened, already-answered card
  // never shows an empty re-submittable form.
  const [seeding, setSeeding] = useState(true)

  // Pre-seed answered state from the persisted record so reopening a brief
  // whose questions were already answered shows the done state, not a fresh
  // form. The record is keyed by the REPORT date (one answer set per brief);
  // its presence IS the "answered" signal. Re-runs when the date changes so
  // navigating between briefs reseeds correctly. A read failure leaves the
  // form interactive (lenient read: never block answering on a flaky get).
  useEffect(() => {
    let live = true
    setSeeding(true)
    ;(async () => {
      try {
        const res = await storage.getJSON(`question-answers/${dateStr}.json`)
        if (live && res && res.data && typeof res.data === 'object') {
          setAnswered(true)
        }
      } finally {
        if (live) setSeeding(false)
      }
    })()
    return () => { live = false }
  }, [storage, dateStr])

  if (!Array.isArray(questions) || questions.length === 0) return null

  // Key selection state by question INDEX, not text, so two cards that happen
  // to share question text never share selection state. (The PERSISTED answers
  // object below is still keyed by text — readable for the next-run agent —
  // which dedupe in sanitizeQuestions keeps collision-free.)
  const allAnswered = questions.every((q, qi) => {
    const p = picks[qi]
    return q.multiSelect ? Array.isArray(p) && p.length > 0 : !!p
  })

  const choose = (qi, q, label) => {
    if (answered) return
    setPicks((prev) => {
      if (q.multiSelect) {
        const cur = Array.isArray(prev[qi]) ? prev[qi] : []
        const next = cur.includes(label)
          ? cur.filter((l) => l !== label)
          : [...cur, label]
        return { ...prev, [qi]: next }
      }
      return { ...prev, [qi]: label }
    })
  }

  const submit = async () => {
    if (!allAnswered || answered || saving) return
    const answers = {}
    questions.forEach((q, qi) => {
      const p = picks[qi]
      answers[q.question] = Array.isArray(p) ? p.join(', ') : (p || '')
    })
    const body = {
      report_date: dateStr,
      answered_at: new Date().toISOString(),
      answers,
      questions,
    }
    setSaving(true)
    setError('')
    const path = `question-answers/${dateStr}.json`
    try {
      // Bare object on a .json path -> stored as-is (no envelope). Keyed by the
      // REPORT date so a re-open overwrites rather than piling duplicates.
      // durableWrite resolves only on a durable outcome: 'synced' (server
      // accepted) or 'queued' (outboxed offline, guaranteed retry) — both mean
      // the next-run agent will be able to read these answers. A fatal server
      // refusal (413/400/403) REJECTS here, so we never flip to "Saved" over a
      // write the server threw away. This is the honest signal the old re-read
      // gate had to reconstruct, now built into the write itself.
      await storage.putJSON(path, body)
      setAnswered(true)
      window.mobius?.signal?.('feedback_given', { date: dateStr, signal: 'questions' })
    } catch {
      // A fatal DurableWriteError lands here. Keep the form interactive and
      // surface retry — never claim "Saved" on a write the server refused. (An
      // offline write does NOT reach this branch: it resolves 'queued'.)
      setError(
        navigator.onLine === false
          ? 'You’re offline — reconnect to send these answers.'
          : 'Could not save your answers — tap to try again.'
      )
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className={`rf-rq${answered ? ' rf-rq--answered' : ''}`}>
      <p className="rf-rq__title">A few questions for tomorrow night</p>
      <p className="rf-rq__note">
        Your answers guide my next run — they won’t change this brief.
      </p>
      {questions.map((q, qi) => {
        const isMulti = q.multiSelect
        const cur = picks[qi]
        const selected = (label) =>
          isMulti ? (Array.isArray(cur) && cur.includes(label)) : cur === label
        return (
          <div key={qi} className="rf-rq__q">
            {q.header && <div className="rf-rq__header">{q.header}</div>}
            <div className="rf-rq__text">{q.question}</div>
            {!answered && (
              <div className="rf-rq__hint">
                {isMulti ? 'Select all that apply' : 'Choose one'}
              </div>
            )}
            <div
              className="rf-rq__opts"
              role={isMulti ? 'group' : 'radiogroup'}
              aria-label={q.question}
            >
              {q.options.map((opt, oi) => {
                const on = selected(opt.label)
                const dim = answered && !on
                return (
                  <button
                    key={oi}
                    type="button"
                    role={isMulti ? 'checkbox' : 'radio'}
                    aria-checked={on}
                    className={`rf-rq__opt rf-pressable${on ? ' rf-rq__opt--on' : ''}${dim ? ' rf-rq__opt--dim' : ''}`}
                    onClick={answered ? undefined : () => choose(qi, q, opt.label)}
                    disabled={answered}
                    title={opt.description || ''}
                  >
                    {opt.label}
                  </button>
                )
              })}
            </div>
          </div>
        )
      })}
      {answered ? (
        <div className="rf-rq__done">Saved — I’ll use this for tomorrow night’s run.</div>
      ) : (
        <>
          <button
            type="button"
            className="rf-rq__submit rf-pressable"
            onClick={submit}
            disabled={!allAnswered || saving || seeding}
          >
            {saving ? 'Saving…' : 'Save for next time'}
          </button>
          {error && <div className="rf-rq__error" role="alert">{error}</div>}
        </>
      )}
    </div>
  )
}
