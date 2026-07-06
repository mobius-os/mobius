import { useCallback, useEffect, useState } from 'react'
import {
  DEFAULT_CRON,
  DEFAULT_HOUR,
  DEFAULT_MODEL,
  DEFAULT_PROVIDER,
  DEFAULT_VERBOSITY,
  FALLBACK_MODEL_GROUPS,
  VERBOSITY_OPTIONS,
} from '../constants.js'
import { buildCron, hourClockLabel, hourToTimeValue, parseCronHour } from '../domain.js'
import { fetchModelConfig } from '../providers.js'

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export function SettingsTab({ appId, storage, token }) {
  const [hour, setHour] = useState(DEFAULT_HOUR)
  const [excludeApps, setExcludeApps] = useState([])
  const [settingsExtra, setSettingsExtra] = useState({})
  const [provider, setProvider] = useState(DEFAULT_PROVIDER)
  const [model, setModel] = useState(DEFAULT_MODEL)
  const [verbosity, setVerbosity] = useState(DEFAULT_VERBOSITY)
  const [focus, setFocus] = useState('')
  const [avoid, setAvoid] = useState('')
  const [modelGroups, setModelGroups] = useState(null)
  const [connectedProviders, setConnectedProviders] = useState(null)
  // The raw cron we loaded — when it's a custom shape parseCronHour can't
  // represent (a non-zero minute, multiple hours), we surface it read-only
  // rather than silently rewriting it to "0 <h> * * *" on the next save.
  const [rawCron, setRawCron] = useState(DEFAULT_CRON)
  const [cronIsCustom, setCronIsCustom] = useState(false)
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [toast, setToast] = useState('')
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const res = await storage.getJSON('settings.json')
      if (cancelled) return
      const s = res.data && typeof res.data === 'object' ? res.data : null
      if (s) {
        setSettingsExtra(s)
        const parsedHour = parseCronHour(s.cron)
        if (parsedHour != null) {
          setHour(parsedHour)
          setCronIsCustom(false)
        } else if (typeof s.cron === 'string' && s.cron.trim()) {
          // Hand-edited / multi-hour cron — keep it, show it read-only.
          setRawCron(s.cron)
          setCronIsCustom(true)
        } else if (Number.isFinite(s.hour) && s.hour >= 0 && s.hour <= 23) {
          // Legacy seed shape used hour/minute/timezone. Preserve it as a
          // readable default, then save in the cron shape the runner expects.
          setHour(s.hour)
          setCronIsCustom(false)
        }
        if (Array.isArray(s.exclude_apps)) setExcludeApps(s.exclude_apps)
        if (typeof s.provider === 'string' && s.provider.trim()) {
          setProvider(s.provider.trim())
        }
        if (typeof s.model === 'string' && s.model.trim()) {
          setModel(s.model.trim())
        }
        const vOpt = VERBOSITY_OPTIONS.find((o) => o.id === s.verbosity)
        if (vOpt) setVerbosity(vOpt.id)
        if (typeof s.focus === 'string') setFocus(s.focus)
        if (typeof s.avoid === 'string') setAvoid(s.avoid)
      }
      // res.notFound (first run) -> keep the 06:00 / standard defaults.
      setLoading(false)
    })()
    return () => { cancelled = true }
  }, [storage])

  useEffect(() => {
    let cancelled = false
    fetchModelConfig(token)
      .then(({ connected, models }) => {
        if (cancelled) return
        setConnectedProviders(connected)
        setModelGroups(models)
      })
      .catch(() => {
        if (cancelled) return
        setModelGroups(FALLBACK_MODEL_GROUPS)
      })
    return () => { cancelled = true }
  }, [token])

  const onTimeChange = useCallback((e) => {
    // <input type="time"> can be cleared to "" -> NaN. Drop NaN so we never
    // write a corrupt cron; the input repaints with the last good value.
    const [hStr] = e.target.value.split(':')
    const h = Number(hStr)
    if (Number.isFinite(h) && h >= 0 && h <= 23) {
      setHour(h)
      setCronIsCustom(false) // editing the hour adopts the standard shape
    }
  }, [])

  const save = useCallback(async () => {
    if (saving) return
    setSaving(true)
    setError('')
    setToast('')
    // Preserve a custom cron verbatim if the user never touched the hour;
    // otherwise write the standard "0 <h> * * *".
    const cron = cronIsCustom ? rawCron : buildCron(hour)
    try {
      // durableWrite resolves on a durable outcome — 'synced' (server accepted)
      // or 'queued' (outboxed offline, guaranteed retry). Both are genuinely
      // saved, so either flips the picker to "Saved ✓": a queued schedule WILL
      // reach the server, and if the queue ever fatally fails on drain,
      // onDeadLetter (wired on App mount) surfaces that asynchronously. Only a
      // fatal server refusal (413/400/403) rejects, dropping into catch below.
      await storage.putJSON('settings.json', {
        ...settingsExtra,
        cron,
        hour,
        minute: 0,
        timezone: settingsExtra.timezone ?? null,
        exclude_apps: excludeApps,
        provider: provider || settingsExtra.provider || DEFAULT_PROVIDER,
        model: model || settingsExtra.model || null,
        effort: settingsExtra.effort ?? null,
        verbosity,
        focus: focus.trim() || null,
        avoid: avoid.trim() || null,
      })
      setToast('Saved ✓')
      setTimeout(() => setToast(''), 2600)
    } catch {
      // A fatal DurableWriteError (the server refused the write) — never a mere
      // outage, which would have resolved 'queued'. Surface a plain save error.
      setError('Could not save — try again.')
    } finally {
      setSaving(false)
    }
  }, [saving, cronIsCustom, rawCron, hour, excludeApps, provider, model, verbosity, focus, avoid, settingsExtra, storage])

  if (loading) {
    return (
      <div className="rf-loading-wrap">
        <span className="rf-spinner" aria-hidden="true" />
        <div>Loading settings…</div>
      </div>
    )
  }

  return (
    <div className="rf-settings-wrap rf-rise">
      <div className="rf-settings-card">
        <div className="rf-section-head">
          <span className="rf-section-icon" aria-hidden="true">⏰</span>
          <h2 className="rf-section-label">When it runs</h2>
        </div>
        <p className="rf-note">
          Pick the hour your morning brief should be ready. Reflection writes it
          overnight so it’s waiting when you wake.
        </p>
        {cronIsCustom ? (
          <div className="rf-custom-cron-note">
            You have a custom schedule set (<code>{rawCron}</code>). Pick an
            hour below to switch to a simple daily time, or leave it as-is.
            <div className="rf-time-row">
              <input
                type="time"
                step="3600"
                className="rf-time-input"
                value={hourToTimeValue(hour)}
                onChange={onTimeChange}
                aria-label="Daily brief time"
              />
              <span className="rf-note">on the hour, every day</span>
            </div>
          </div>
        ) : (
          <div className="rf-time-row">
            <input
              type="time"
              step="3600"
              className="rf-time-input"
              value={hourToTimeValue(hour)}
              onChange={onTimeChange}
              aria-label="Daily brief time"
            />
            <span className="rf-note">
              ready around <strong className="rf-note-strong">{hourClockLabel(hour)}</strong>, every day
            </span>
          </div>
        )}
        <div className="rf-schedule-hint">
          <span aria-hidden="true">💡</span>
          <span>
            Schedule changes take effect after the reflection agent re-installs
            its overnight job — usually by the next run. The app saves your
            preference; the agent picks it up from there.
          </span>
        </div>
      </div>

      <div className="rf-settings-card">
        <div className="rf-section-head">
          <span className="rf-section-icon" aria-hidden="true">🤖</span>
          <h2 className="rf-section-label">Nightly model</h2>
        </div>
        <p className="rf-note">
          The model Reflection uses for the overnight pass. It runs its own
          procedure with the default skill.
        </p>
        {modelGroups === null ? (
          <div className="rf-note">Loading models…</div>
        ) : modelGroups.length === 0 ? (
          // Models API unavailable — fall back to letting the CLI choose.
          <div className="rf-note">
            Model list unavailable. Reflection will use the CLI's default model
            for your account.
          </div>
        ) : (
          <>
            <select
              className="rf-select"
              value={model ? `${provider}\t${model}` : `${provider}\t`}
              onChange={(e) => {
                const idx = e.target.value.indexOf('\t')
                const nextProvider = e.target.value.slice(0, idx)
                const nextModel = e.target.value.slice(idx + 1) || null
                if (nextProvider) {
                  setProvider(nextProvider)
                  setModel(nextModel)
                }
              }}
              aria-label="Reflection model"
            >
              <option value={`${provider}\t`}>Provider default</option>
              {modelGroups.map((group) => {
                const isConnected = !connectedProviders || connectedProviders.has(group.key)
                return (
                  <optgroup
                    key={group.key}
                    label={`${group.label}${isConnected ? '' : ' (not connected)'}`}
                  >
                    {group.models.map((m) => {
                      const on = provider === group.key && model === m.id
                      return (
                        <option
                          key={`${group.key}-${m.id}`}
                          value={`${group.key}\t${m.id}`}
                          disabled={!isConnected && !on}
                        >
                          {m.name}
                        </option>
                      )
                    })}
                  </optgroup>
                )
              })}
            </select>
            <div className="rf-meta">
              {(modelGroups.find((group) => group.key === provider)?.label || provider)}
              {' · '}
              {model || 'provider default'}
            </div>
          </>
        )}
      </div>

      <div className="rf-settings-card">
        <div className="rf-section-head">
          <span className="rf-section-icon" aria-hidden="true">📝</span>
          <h2 className="rf-section-label">Brief style</h2>
        </div>
        <p className="rf-note">
          How long and how detailed you'd like the morning brief. The reflection
          skill honors this when writing tonight's report.
        </p>
        <div className="rf-verbosity-row">
          {VERBOSITY_OPTIONS.map((opt) => (
            <button
              key={opt.id}
              className={`rf-verb-btn${verbosity === opt.id ? ' is-active' : ''} rf-pressable`}
              onClick={() => setVerbosity(opt.id)}
              aria-pressed={verbosity === opt.id}
            >
              {opt.label}
            </button>
          ))}
        </div>
        <p className="rf-verb-hint">
          {VERBOSITY_OPTIONS.find((o) => o.id === verbosity)?.hint}
        </p>
      </div>

      <div className="rf-settings-card">
        <div className="rf-section-head">
          <span className="rf-section-icon" aria-hidden="true">🧭</span>
          <h2 className="rf-section-label">Tonight's steering</h2>
        </div>
        <p className="rf-note">
          Optional nudges the reflection agent reads before deciding what to cover.
          Leave blank to let it choose freely.
        </p>
        <label className="rf-note" style={{ display: 'block', marginBottom: 4 }}>
          <span className="rf-note-strong">Prioritise</span> — topics or apps to pay extra attention to
        </label>
        <textarea
          className="rf-textarea"
          value={focus}
          onChange={(e) => setFocus(e.target.value)}
          placeholder={'e.g. "look for regressions in the Habits app" or "I\'ve been researching climate policy"'}
          aria-label="Topics to prioritise tonight"
        />
        <label className="rf-note" style={{ display: 'block', marginTop: 10, marginBottom: 4 }}>
          <span className="rf-note-strong">Skip</span> — topics or apps to leave out of tonight's brief
        </label>
        <textarea
          className="rf-textarea"
          value={avoid}
          onChange={(e) => setAvoid(e.target.value)}
          placeholder={'e.g. "skip the workout app" or "don\'t mention work projects"'}
          aria-label="Topics to skip tonight"
        />
      </div>

      <div className="rf-save-row">
        <button className="rf-save-btn rf-pressable" onClick={save} disabled={saving}>
          {saving ? 'Saving…' : 'Save settings'}
        </button>
        {toast && <span className="rf-toast">{toast}</span>}
        {error && <span className="rf-error-toast">{error}</span>}
      </div>
    </div>
  )
}
