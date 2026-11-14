/* Settings → Notifications elsewhere: a summary row on the overview and its own
   page. A channel copies owner notifications to Slack, Discord, ntfy or any
   webhook, exactly when a phone push would be sent. The link works like a
   password: it goes in once and is never shown again. */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { Alert } from '@openai/apps-sdk-ui/components/Alert'
import { Bell } from '@openai/apps-sdk-ui/components/Icon'
import { Switch } from '@openai/apps-sdk-ui/components/Switch'
import { api } from '../../api/client.js'
import { appQueries } from '../../hooks/queries.js'

const SERVICES = [
  {
    kind: 'slack',
    name: 'Slack',
    placeholder: 'https://hooks.slack.com/services/…',
    // The same shapes the server checks; this only gives earlier feedback.
    complete: /^https:\/\/hooks\.slack\.com\/services\/T[A-Z0-9]+\/B[A-Z0-9]+\/[A-Za-z0-9]{8,}\/?$/,
    incomplete: 'This Slack link looks cut off. Use the Copy button next to your channel’s webhook.',
    steps: [
      <>Open <b>api.slack.com/apps</b> and choose <b>Create New App</b> → <b>From scratch</b>. Name it Möbius and pick your workspace.</>,
      <>In the app’s menu open <b>Incoming Webhooks</b> and switch it <b>On</b>. No scopes are needed.</>,
      <>Scroll down, click <b>Add New Webhook</b>, pick a channel and <b>Allow</b>. If Slack asks for approval, a workspace admin must approve it.</>,
      <>Click <b>Copy</b> next to the new link starting with hooks.slack.com/services/. Ignore Slack’s “agent setup” prompt.</>,
    ],
  },
  {
    kind: 'discord',
    name: 'Discord',
    placeholder: 'https://discord.com/api/webhooks/…',
    complete: /^https:\/\/(ptb\.|canary\.)?discord(app)?\.com\/api\/webhooks\/\d+\/[A-Za-z0-9_-]{8,}\/?$/,
    incomplete: 'This Discord link looks cut off. Use Copy Webhook URL in Discord.',
    steps: [
      <>In Discord, open the channel’s <b>Edit Channel</b> → <b>Integrations</b> → <b>Webhooks</b>. You need the Manage Webhooks permission.</>,
      <>Click <b>New Webhook</b>, then <b>Copy Webhook URL</b>.</>,
    ],
  },
  {
    kind: 'ntfy',
    name: 'ntfy',
    placeholder: 'https://ntfy.sh/your-topic',
    complete: /^https:\/\/[^/\s]+(\/[^/\s]+)*\/[A-Za-z0-9_-]+\/?$/,
    incomplete: 'Add your topic to the link, like https://ntfy.sh/your-topic.',
    steps: [
      <>Install the <b>ntfy</b> app on your phone and subscribe to a hard-to-guess topic.</>,
      <>Paste <b>https://ntfy.sh/</b> followed by that topic.</>,
    ],
  },
  {
    kind: 'webhook',
    name: 'Webhook',
    placeholder: 'https://example.com/hook',
    complete: /^https:\/\/\S+$/,
    incomplete: 'Use the full https:// address.',
    steps: [
      <>Paste any <b>https</b> address that accepts a JSON POST. Möbius sends the title, a link back, the source, and the message text if you allow it.</>,
    ],
  },
]
const SERVICE = Object.fromEntries(SERVICES.map((service) => [service.kind, service]))

function ServiceIcon({ kind, size = 20 }) {
  if (kind === 'slack') {
    return (
      <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
        <rect x="3" y="9.5" width="8" height="3.2" rx="1.6" fill="#36C5F0" />
        <rect x="9.5" y="3" width="3.2" height="8" rx="1.6" fill="#2EB67D" />
        <rect x="13" y="11.3" width="8" height="3.2" rx="1.6" fill="#ECB22E" />
        <rect x="11.3" y="13" width="3.2" height="8" rx="1.6" fill="#E01E5A" />
      </svg>
    )
  }
  if (kind === 'discord') {
    return (
      <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
        <rect x="2" y="4" width="20" height="16" rx="6" fill="#5865F2" />
        <circle cx="9" cy="12.5" r="1.7" fill="#fff" />
        <circle cx="15" cy="12.5" r="1.7" fill="#fff" />
      </svg>
    )
  }
  if (kind === 'ntfy') {
    return (
      <svg viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
        <rect x="2" y="2" width="20" height="20" rx="6" fill="#2C9C6E" />
        <path d="M12 6.5a4 4 0 0 0-4 4v3l-1.3 1.8h10.6L16 13.5v-3a4 4 0 0 0-4-4Zm-1.6 10h3.2a1.6 1.6 0 0 1-3.2 0Z" fill="#fff" />
      </svg>
    )
  }
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" aria-hidden="true">
      <path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" />
      <path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" />
    </svg>
  )
}

function detectKind(url) {
  try {
    const { hostname, pathname } = new URL(url.trim())
    if (hostname === 'hooks.slack.com') return 'slack'
    if (/(^|\.)discord(app)?\.com$/.test(hostname) && pathname.includes('/api/webhooks/')) return 'discord'
    if (hostname === 'ntfy.sh') return 'ntfy'
  } catch {
    // Not a URL yet.
  }
  return null
}

function ago(iso) {
  const then = new Date(iso).getTime()
  if (!Number.isFinite(then)) return ''
  const minutes = Math.round((Date.now() - then) / 60000)
  if (minutes < 1) return 'just now'
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} h ago`
  return new Date(iso).toLocaleDateString(undefined, { day: 'numeric', month: 'short' })
}

async function errorText(response, fallback) {
  try {
    const body = await response.json()
    if (typeof body?.detail === 'string') return body.detail
  } catch {
    // Fall through to the generic message.
  }
  return fallback
}

/** The owner's channels, refreshed whenever `active` turns on. */
function useChannels(active) {
  const [channels, setChannels] = useState(null)
  const load = useCallback(async () => {
    try {
      const response = await api.push.channels.list()
      if (!response?.ok) return
      const body = await response.json()
      setChannels(Array.isArray(body) ? body : [])
    } catch {
      // Settings stays usable; the list simply shows nothing new.
    }
  }, [])
  useEffect(() => {
    if (active) load()
  }, [active, load])
  return [channels, load, setChannels]
}

function useApps(active) {
  const query = appQueries.list.useQuery({ enabled: active })
  return useMemo(
    () => (Array.isArray(query.data) ? query.data : [])
      .filter((app) => app && app.id != null && app.name)
      .sort((a, b) => a.name.localeCompare(b.name)),
    [query.data],
  )
}

/* ── overview row ─────────────────────────────────────────────────────── */

function sendsText(channel, appNames) {
  const scope = scopeText(channel, appNames)
  return `${scope === 'Everything' ? 'Everything' : `${scope} only`} · ${channel.includeBody ? 'with text' : 'titles only'}`
}

const STACK_MAX = 3

export function NotificationChannelsSummary({ active = true, onOpen }) {
  const [channels] = useChannels(active)
  const apps = useApps(active)
  const appNames = useMemo(() => new Map(apps.map((app) => [app.id, app.name])), [apps])
  const list = channels || []
  const failing = list.filter((channel) => channel.lastError).length
  // One stacked icon per service; a red dot when any of its links fails.
  const kinds = [...new Set(list.map((channel) => channel.kind))]
  const broken = new Set(list.filter((channel) => channel.lastError).map((channel) => channel.kind))
  const title = channels == null ? 'Checking…'
    : list.length ? list.map((channel) => channel.label).join(', ') : 'Not set up'
  const sends = [...new Set(list.map((channel) => sendsText(channel, appNames)))]
  const detail = !list.length ? 'Slack, Discord, ntfy or any webhook'
    : failing ? `${failing === 1 ? 'A link' : `${failing} links`} stopped working`
      : sends.length === 1 ? sends[0] : `${list.length} channels`
  return (
    <section className="settings-group" aria-labelledby="settings-channels-title">
      <div className="settings-group__head">
        <div className="settings-group__heading">
          <h2 className="settings-group__title" id="settings-channels-title">Notifications elsewhere</h2>
          <p className="settings-group__desc">Get Möbius notifications in Slack, Discord and more when you’re away.</p>
        </div>
      </div>
      <div className="settings-list">
        {/* Opens its own page, so no arrow: arrows in Settings mean "expands here". */}
        <button type="button" className="settings-item" onClick={onOpen}
          aria-label={`Notifications elsewhere: ${title}. ${detail}`}>
          {kinds.length ? (
            <span className="nc-stack" aria-hidden="true">
              {kinds.slice(0, STACK_MAX).map((kind) => (
                <span key={kind} className="nc-stack__icon" title={SERVICE[kind]?.name || kind}>
                  <ServiceIcon kind={kind} size={17} />
                  {broken.has(kind) && <span className="nc-stack__dot" />}
                </span>
              ))}
              {kinds.length > STACK_MAX && <span className="nc-stack__icon nc-stack__more">+{kinds.length - STACK_MAX}</span>}
            </span>
          ) : (
            <span className="settings-item__icon" aria-hidden="true"><Bell width={18} height={18} /></span>
          )}
          <span className="settings-item__body">
            <span className="settings-item__title">{title}</span>
            <span className={`settings-item__sub${failing ? ' nc-bad' : ''}`}>{detail}</span>
          </span>
          {failing > 0 && <span className="settings-badge settings-badge--danger">{failing} need{failing === 1 ? 's' : ''} you</span>}
        </button>
      </div>
    </section>
  )
}

/* ── shared controls ──────────────────────────────────────────────────── */

function Segmented({ label, options, value, onChange }) {
  return (
    <div className="nc-seg" role="radiogroup" aria-label={label}>
      {options.map((option) => (
        <button key={option.value} type="button" role="radio" aria-checked={value === option.value}
          className={`nc-seg__option${value === option.value ? ' nc-seg__option--on' : ''}`}
          onClick={() => onChange(option.value)}>
          {option.label}
        </button>
      ))}
    </div>
  )
}

/** Which notifications go to a channel, and whether their text goes along. */
function SendOptions({ apps, social, appIds, includeBody, onChange }) {
  // "Choose apps" with only Social ticked saves the same list as "Only Social",
  // so remember the owner's pick; otherwise that first tap changes nothing.
  const [choosing, setChoosing] = useState(false)
  const onlySocial = social && appIds?.length === 1 && appIds[0] === social.id
  const scope = appIds == null ? 'all' : onlySocial && !choosing ? 'social' : 'apps'
  const chosen = new Set(appIds || [])
  const options = [
    ...(social ? [{ value: 'social', label: 'Only Social' }] : []),
    { value: 'apps', label: 'Choose apps' },
    { value: 'all', label: 'Everything' },
  ]
  const pick = (next) => {
    setChoosing(next === 'apps')
    if (next === 'all') onChange({ appIds: null })
    else if (next === 'social') onChange({ appIds: [social.id] })
    else onChange({ appIds: [...chosen] })
  }
  const toggle = (id) => {
    const next = new Set(chosen)
    if (next.has(id)) next.delete(id)
    else next.add(id)
    onChange({ appIds: [...next] })
  }
  return (
    <div className="nc-options">
      <Segmented label="What to send" options={options} value={scope} onChange={pick} />
      {scope === 'apps' && (
        <div className="nc-chips">
          {apps.map((app) => (
            <button key={app.id} type="button" aria-pressed={chosen.has(app.id)}
              className={`nc-chip${chosen.has(app.id) ? ' nc-chip--on' : ''}`} onClick={() => toggle(app.id)}>
              {app.name}
            </button>
          ))}
        </div>
      )}
      <div className="nc-switch-row">
        <span>
          <span className="nc-switch-row__title">Include message text</span>
          <span className="nc-hint">
            {includeBody
              ? 'Message text will also be kept by the service you send to.'
              : 'Off: only the title, like “Message from @name”, and a link back.'}
          </span>
        </span>
        <Switch className="settings-switch" aria-label="Include message text" checked={includeBody}
          onCheckedChange={(next) => onChange({ includeBody: next })} />
      </div>
    </div>
  )
}

function health(channel) {
  if (channel.lastError) return { tone: 'bad', text: channel.lastError }
  if (channel.lastSentAt) return { tone: 'ok', text: `Delivered ${ago(channel.lastSentAt)}` }
  return { tone: 'idle', text: 'Nothing sent yet' }
}

function scopeText(channel, appNames) {
  if (channel.appIds == null) return 'Everything'
  const names = channel.appIds.map((id) => appNames.get(id)).filter(Boolean)
  if (!names.length) return 'No apps chosen'
  return names.length <= 2 ? names.join(', ') : `${names.length} apps`
}

/* ── guided add ───────────────────────────────────────────────────────── */

const EMPTY_DRAFT = { kind: 'slack', url: '', label: '', appIds: undefined, includeBody: false }

function GuidedAdd({ apps, social, onDone, onCancel }) {
  const [draft, setDraft] = useState(EMPTY_DRAFT)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const service = SERVICE[draft.kind]
  // Social is why this exists, so it is the first choice when installed.
  const appIds = draft.appIds === undefined ? (social ? [social.id] : null) : draft.appIds
  const url = draft.url.trim()
  const complete = url && service.complete.test(url)
  const step = !url ? 2 : complete ? 3 : 2

  const setUrl = (value) => {
    const detected = detectKind(value)
    setDraft((current) => ({ ...current, url: value, kind: detected || current.kind }))
    setError('')
  }

  const submit = async (event) => {
    event.preventDefault()
    if (!complete) return
    setBusy(true)
    setError('')
    try {
      const response = await api.push.channels.add({
        url, kind: draft.kind, label: draft.label.trim() || null, app_ids: appIds, include_body: draft.includeBody,
      })
      if (!response?.ok) {
        setError(await errorText(response, 'That link could not be added.'))
        setBusy(false)
        return
      }
      onDone(await response.json())
    } catch {
      setError('That link could not be added. Try again.')
      setBusy(false)
    }
  }

  return (
    <form className="settings-card nc-add" onSubmit={submit} aria-labelledby="nc-add-title">
      <div className="nc-progress" aria-hidden="true">
        {[1, 2, 3].map((n) => <span key={n} className={n <= step ? 'nc-progress--on' : ''} />)}
      </div>
      <h3 className="nc-add__title" id="nc-add-title">Add a place to send notifications</h3>

      <p className="nc-step">1 · Where should they go?</p>
      <div className="nc-tiles" role="radiogroup" aria-label="Service">
        {SERVICES.map((option) => (
          <button key={option.kind} type="button" role="radio" aria-checked={draft.kind === option.kind}
            className={`nc-tile${draft.kind === option.kind ? ' nc-tile--on' : ''}`}
            onClick={() => setDraft({ ...draft, kind: option.kind })}>
            <span className="settings-item__icon"><ServiceIcon kind={option.kind} /></span>
            {option.name}
          </button>
        ))}
      </div>
      <ol className="nc-guide">{service.steps.map((text, index) => <li key={index}>{text}</li>)}</ol>

      <label className="nc-step" htmlFor="nc-url">2 · Paste the link</label>
      <input id="nc-url" className={`nc-input${url ? (complete ? ' nc-input--ok' : ' nc-input--bad') : ''}`}
        type="url" autoComplete="off" spellCheck={false} placeholder={service.placeholder}
        value={draft.url} onChange={(event) => setUrl(event.target.value)} aria-describedby="nc-url-check" />
      <p id="nc-url-check" className={`nc-check${url ? (complete ? ' nc-check--ok' : ' nc-check--bad') : ''}`} role="status">
        {url ? (complete ? 'Looks like a complete link.' : service.incomplete) : 'It works like a password: Möbius keeps it encrypted and never shows it again.'}
      </p>
      <input className="nc-input nc-input--small" type="text" maxLength={80} aria-label="Name (optional)"
        placeholder={`Name (optional), like ${draft.kind === 'discord' ? '#alerts' : '#mobius-test'}`}
        value={draft.label} onChange={(event) => setDraft({ ...draft, label: event.target.value })} />

      <p className="nc-step">3 · What to send</p>
      <SendOptions apps={apps} social={social} appIds={appIds} includeBody={draft.includeBody}
        onChange={(change) => setDraft({ ...draft, ...change })} />

      {error && <Alert color="danger" variant="soft" description={error} />}
      <div className="settings-actions">
        <button className="settings__btn settings__btn--sm" type="submit" disabled={busy || !complete}>
          {busy ? 'Connecting…' : 'Connect and send test'}
        </button>
        {onCancel && <button className="settings__btn settings__btn--outline settings__btn--sm" type="button" onClick={onCancel}>Cancel</button>}
      </div>
    </form>
  )
}

/* ── replace a link ───────────────────────────────────────────────────── */

/** Swap a channel's link in place; its other settings and history stay. */
function ReplaceLink({ channel, onDone, onCancel }) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const url = value.trim()
  // Like the server, a link no service claims is a generic webhook.
  const service = SERVICE[detectKind(url) || (url ? 'webhook' : channel.kind)]
  const complete = url && service.complete.test(url)
  const fieldId = `nc-replace-${channel.id}`

  const submit = async (event) => {
    event.preventDefault()
    if (!complete) return
    setBusy(true)
    setError('')
    try {
      const response = await api.push.channels.update(channel.id, { url })
      if (!response?.ok) {
        setError(await errorText(response, 'That link could not be saved.'))
        setBusy(false)
        return
      }
      onDone(await response.json())
    } catch {
      setError('That link could not be saved. Try again.')
      setBusy(false)
    }
  }

  return (
    <form className="nc-replace" onSubmit={submit}>
      <label className="nc-step" htmlFor={fieldId}>New link</label>
      <input id={fieldId} className={`nc-input${url ? (complete ? ' nc-input--ok' : ' nc-input--bad') : ''}`}
        type="url" autoComplete="off" spellCheck={false} autoFocus placeholder={service.placeholder}
        value={value} onChange={(event) => { setValue(event.target.value); setError('') }}
        aria-describedby={`${fieldId}-check`} />
      <p id={`${fieldId}-check`} className={`nc-check${url ? (complete ? ' nc-check--ok' : ' nc-check--bad') : ''}`} role="status">
        {url ? (complete ? 'Looks like a complete link.' : service.incomplete)
          : 'Your other settings stay. The old link stops being used.'}
      </p>
      {error && <Alert color="danger" variant="soft" description={error} />}
      <div className="settings-actions">
        <button className="settings__btn settings__btn--sm" type="submit" disabled={busy || !complete}>
          {busy ? 'Saving…' : 'Save and send test'}
        </button>
        <button className="settings__btn settings__btn--outline settings__btn--sm" type="button"
          disabled={busy} onClick={onCancel}>Cancel</button>
      </div>
    </form>
  )
}

/* ── the page ─────────────────────────────────────────────────────────── */

export default function NotificationChannelsPage({ active = true }) {
  const [channels, load, setChannels] = useChannels(active)
  const apps = useApps(active)
  const social = apps.find((app) => app.slug === 'social') || null
  const appNames = useMemo(() => new Map(apps.map((app) => [app.id, app.name])), [apps])
  const [adding, setAdding] = useState(false)
  const [open, setOpen] = useState(null)
  const [replacing, setReplacing] = useState(null)
  const [busy, setBusy] = useState(null)
  const [notice, setNotice] = useState(null)
  const list = channels || []
  const showAdd = adding || (channels != null && list.length === 0)

  const test = async (channel) => {
    setBusy(`test:${channel.id}`)
    setNotice(null)
    try {
      const response = await api.push.channels.test(channel.id)
      const body = response?.ok ? await response.json() : null
      setNotice(body?.ok
        ? { color: 'success', text: `Sent a test message to ${channel.label}.` }
        : { color: 'danger', text: `${channel.label}: ${body?.error || 'the test message was not sent.'}` })
    } catch {
      setNotice({ color: 'danger', text: `${channel.label}: the test message was not sent.` })
    }
    await load()
    setBusy(null)
  }

  const update = async (channel, change) => {
    const payload = {}
    if ('appIds' in change) {
      if (change.appIds == null) payload.all_notifications = true
      else payload.app_ids = change.appIds
    }
    if ('includeBody' in change) payload.include_body = change.includeBody
    setChannels((rows) => rows.map((row) => (row.id === channel.id ? { ...row, ...change } : row)))
    try {
      const response = await api.push.channels.update(channel.id, payload)
      if (!response?.ok) setNotice({ color: 'danger', text: `Couldn't change ${channel.label}. Try again.` })
    } catch {
      setNotice({ color: 'danger', text: `Couldn't change ${channel.label}. Try again.` })
    }
    await load()
  }

  const remove = async (channel) => {
    setBusy(`remove:${channel.id}`)
    setNotice(null)
    try {
      const response = await api.push.channels.remove(channel.id)
      if (!response?.ok) setNotice({ color: 'danger', text: `Couldn't remove ${channel.label}. Try again.` })
    } catch {
      setNotice({ color: 'danger', text: `Couldn't remove ${channel.label}. Try again.` })
    }
    setOpen(null)
    await load()
    setBusy(null)
  }

  const replaced = async (channel) => {
    setReplacing(null)
    await load()
    await test(channel)
  }

  const added = async (channel) => {
    setAdding(false)
    await load()
    await test(channel)
  }

  return (
    <div className="nc-page">
      <p className="settings-group__desc nc-page__lede">
        Notifications are copied here exactly when your phone would get them, so nothing is sent while you’re looking at that app.
      </p>

      {list.length > 0 && (
        <section className="settings-group nc-page__group" aria-labelledby="nc-connected-title">
          <div className="settings-group__head">
            <div className="settings-group__heading">
              <h2 className="settings-group__title" id="nc-connected-title">Connected</h2>
            </div>
            {!showAdd && (
              <button className="settings__btn settings__btn--outline settings__btn--sm" type="button"
                onClick={() => { setAdding(true); setNotice(null) }}>
                Add
              </button>
            )}
          </div>
          <div className="settings-list">
            {list.map((channel) => {
              const state = health(channel)
              const expanded = open === channel.id
              return (
                <div key={channel.id}>
                  <button type="button" className="settings-item" aria-expanded={expanded}
                    onClick={() => { setOpen(expanded ? null : channel.id); setReplacing(null) }}>
                    <span className="settings-item__icon" aria-hidden="true"><ServiceIcon kind={channel.kind} /></span>
                    <span className="settings-item__body">
                      <span className="settings-item__title">{channel.label}</span>
                      <span className={`settings-item__sub${state.tone === 'bad' ? ' settings-item__sub--wrap nc-bad' : ''}`}>
                        <span className={`nc-dot nc-dot--${state.tone}`} aria-hidden="true" />
                        {state.text} · {scopeText(channel, appNames)} · {channel.includeBody ? 'with text' : 'titles only'}
                      </span>
                    </span>
                    {state.tone === 'bad' && <span className="settings-badge settings-badge--danger">Fix</span>}
                    <span className={`nc-chevron${expanded ? ' nc-chevron--open' : ''}`} aria-hidden="true">›</span>
                  </button>
                  {expanded && (
                    <div className="settings-item-panel nc-panel">
                      {state.tone === 'bad' && (
                        <p className="nc-hint">If the link was deleted in {SERVICE[channel.kind]?.name || 'the service'}, remove this one and add a new link.</p>
                      )}
                      <SendOptions apps={apps} social={social} appIds={channel.appIds} includeBody={channel.includeBody}
                        onChange={(change) => update(channel, change)} />
                      <p className="nc-hint">Link {channel.urlHint}</p>
                      {replacing === channel.id ? (
                        <ReplaceLink channel={channel} onDone={replaced} onCancel={() => setReplacing(null)} />
                      ) : (
                      <div className="settings-actions">
                        <button className="settings__btn settings__btn--outline settings__btn--sm" type="button"
                          disabled={!!busy} onClick={() => test(channel)}>
                          {busy === `test:${channel.id}` ? 'Sending…' : 'Send test'}
                        </button>
                        <button className="settings__btn settings__btn--outline settings__btn--sm" type="button"
                          disabled={!!busy} onClick={() => { setReplacing(channel.id); setNotice(null) }}>
                          Replace link
                        </button>
                        <button className="settings__btn settings__btn--outline settings__btn--sm nc-remove" type="button"
                          disabled={!!busy} onClick={() => remove(channel)}>
                          {busy === `remove:${channel.id}` ? 'Removing…' : 'Remove'}
                        </button>
                      </div>
                      )}
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        </section>
      )}

      {notice && <div className="nc-notice"><Alert color={notice.color} variant="soft" description={notice.text} /></div>}

      {showAdd && (
        <section className="settings-group nc-page__group" aria-label="Add a destination">
          <GuidedAdd apps={apps} social={social} onDone={added}
            onCancel={list.length ? () => setAdding(false) : null} />
        </section>
      )}
    </div>
  )
}
