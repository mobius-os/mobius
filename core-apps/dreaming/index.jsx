/* Dreaming — the nightly morning-brief viewer.
 *
 * Lists the dated reports the dreaming agent leaves overnight, tracks a
 * streak, and lets the owner set the run hour and model. Opening a brief
 * shows TWO things stacked: the brief HTML up top (a sandboxed iframe —
 * the agent's static page) and, beneath it, the MORNING CHAT the nightly
 * run opened — the conversation about that brief, live, with a real
 * composer and tappable AskUserQuestion cards. The brief is the read; the
 * chat is where the partner steers the next night.
 *
 * Data contract (unchanged, load-bearing):
 *  - List reports:  GET /api/storage/apps-list/{appId}/reports/   (cursor-paged)
 *  - Read a brief:  GET /api/storage/apps/{appId}/reports/<date>.html  (TEXT)
 *  - settings.json / state.json: JSON via the same storage base.
 *  - Reports render in a sandboxed srcDoc iframe. Sandbox: allow-scripts but
 *    NOT allow-same-origin, so the iframe has a null origin and its scripts
 *    can NOT access the parent's DOM, localStorage, or owner JWT. Scripts
 *    run for the sole purpose of reporting content height via postMessage.
 *    hardenReportHtml injects a CSP + a minimal height-reporter snippet.
 *
 * Brief↔chat link: the nightly run creates the morning chat app-attributed
 * (`POST /api/app-chats` with a Dreaming app token, title "Morning brief —
 * <date>") so the conversation lives HERE, under its brief, and stays out of
 * the owner's drawer history (`GET /api/chats` hides `created_by_app_id`
 * chats by default). The run SHOULD write a sibling
 * `reports/<date>.meta.json` = { "chat_id": "<id>" } so the date maps to a
 * chat without guessing. The app reads that meta with its app token; when a
 * chat_id resolves it mounts the real ChatView via `window.mobius.chat({
 * mount, chatId })` (the embed runs in the shell origin with the owner JWT,
 * which can always read/post an app's chats — and still renders legacy
 * owner-created morning chats through the same path). No chat_id (or no
 * `window.mobius.chat`) → the brief stands alone, gracefully.
 */
import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const PROVIDER_LABELS = {
  claude: 'Claude Code',
  codex: 'OpenAI Codex',
}
const PROVIDER_ORDER = [
  { key: 'claude', label: PROVIDER_LABELS.claude },
  { key: 'codex', label: PROVIDER_LABELS.codex },
]
// When /api/auth/providers/models is unreachable we use an empty fallback
// rather than guessing model IDs. Stale hardcoded IDs would 400 the nightly
// run; letting the CLI use its own account default is always safe.
const FALLBACK_MODEL_GROUPS = []
const DEFAULT_PROVIDER = 'claude'
const DEFAULT_MODEL = null

// The default schedule: 06:00 local -> "0 6 * * *". We only ever let the
// user pick an hour (minute pinned to 0), so the cron field is always
// "0 <h> * * *". parseCronHour / buildCron are the single round-trip
// between the picker's hour and that string.
const DEFAULT_HOUR = 6
const DEFAULT_CRON = '0 6 * * *'

// Pull the hour out of a "0 <h> * * *" cron string. Returns null for
// anything that doesn't match the minute-0, single-hour shape this app
// writes — a hand-edited cron (e.g. "30 6 * * *" or "0 6,18 * * *")
// shouldn't be silently coerced to an hour the picker can't represent;
// the caller falls back to the default and the UI notes it can't show
// a custom schedule rather than lying about one.
function parseCronHour(cron) {
  if (typeof cron !== 'string') return null
  const parts = cron.trim().split(/\s+/)
  if (parts.length !== 5) return null
  const [min, hr, dom, mon, dow] = parts
  if (min !== '0' || dom !== '*' || mon !== '*' || dow !== '*') return null
  if (!/^\d{1,2}$/.test(hr)) return null
  const h = Number(hr)
  if (h < 0 || h > 23) return null
  return h
}

function buildCron(hour) {
  return `0 ${hour} * * *`
}

const REPORT_CSP = [
  "default-src 'none'",
  "script-src 'unsafe-inline'",
  "style-src 'unsafe-inline'",
  'img-src data: blob:',
  'font-src data:',
  "base-uri 'none'",
  "form-action 'none'",
].join('; ')

// Injected into every brief's <head>. Reports scrollHeight to the parent
// via postMessage so the parent can size the iframe without needing
// allow-same-origin (which would give the iframe the shell origin and its
// owner JWT). The script is intentionally tiny — no external deps, no
// network calls. The CSP above allows 'unsafe-inline' scripts precisely
// for this snippet; together with the absence of allow-same-origin the
// iframe's origin is null and it cannot reach the parent's DOM or storage.
const REPORT_HEIGHT_SCRIPT = `<script>
(function(){
  function emit(){
    var h=Math.max(document.body?document.body.scrollHeight:0,
                   document.documentElement.scrollHeight);
    if(h>0)parent.postMessage({type:'dreaming:brief-height',height:h},'*');
  }
  if(document.readyState==='loading'){
    document.addEventListener('DOMContentLoaded',emit);
  } else { emit(); }
  if(typeof ResizeObserver!=='undefined'){
    var ro=new ResizeObserver(emit);
    ro.observe(document.documentElement);
  } else {
    window.addEventListener('resize',emit);
  }
})();
</script>`

export function hardenReportHtml(html) {
  const body = typeof html === 'string' ? html : ''
  const inject = `<meta http-equiv="Content-Security-Policy" content="${REPORT_CSP}">${REPORT_HEIGHT_SCRIPT}`
  if (/<head[\s>]/i.test(body)) return body.replace(/<head([^>]*)>/i, `<head$1>${inject}`)
  if (/<html[\s>]/i.test(body)) return body.replace(/<html([^>]*)>/i, `<html$1><head>${inject}</head>`)
  return `<!doctype html><html><head>${inject}</head><body>${body}</body></html>`
}

// Validate + coerce the in-report question carrier's questions array into
// the exact shape the native card consumes: [{ question, header,
// multiSelect, options:[{label, description}] }]. Anything malformed is
// dropped, not repaired. Caps at 3 questions and 6 options each.
export function sanitizeQuestions(arr) {
  if (!Array.isArray(arr)) return []
  const out = []
  for (const raw of arr) {
    if (out.length >= 3) break
    if (!raw || typeof raw !== 'object') continue
    const question = typeof raw.question === 'string' ? raw.question.trim() : ''
    if (!question) continue
    const opts = Array.isArray(raw.options) ? raw.options : []
    const options = []
    for (const o of opts) {
      if (options.length >= 6) break
      const label = o && typeof o.label === 'string' ? o.label.trim() : ''
      if (!label) continue
      const description = o && typeof o.description === 'string' ? o.description.trim() : ''
      options.push(description ? { label, description } : { label })
    }
    if (options.length === 0) continue
    out.push({
      question,
      header: typeof raw.header === 'string' ? raw.header.trim() : '',
      multiSelect: raw.multiSelect === true,
      options,
    })
  }
  return out
}

// Pull the agent's declarative in-report questions out of the RAW brief
// HTML, returning the HTML with the carrier removed so it never reaches the
// sandboxed iframe. The brief agent emits ONE inert JSON carrier:
//
//   <section class="report-questions" data-report-questions>
//     <h2>…</h2><p class="rq-note">…</p>
//     <script type="application/mobius-questions+json">{ … }</script>
//   </section>
//
// Regex-based (no DOMParser) so it's identical to the news app's copy and
// safe to run before hardenReportHtml. Returns { html, questions }: html
// with the carrier stripped; questions = a validated array (the EXACT shell
// QuestionCard shape) or [] when absent/malformed. Never throws — the brief
// is the floor, a bad carrier just means no cards.
export function extractReportQuestions(html) {
  const empty = { html: typeof html === 'string' ? html : '', questions: [] }
  if (typeof html !== 'string') return empty
  const scriptRe = /<script\b[^>]*type=["']application\/mobius-questions\+json["'][^>]*>([\s\S]*?)<\/script>/i
  const m = html.match(scriptRe)
  let questions = []
  if (m) {
    try {
      const parsed = JSON.parse(m[1].trim())
      questions = sanitizeQuestions(parsed && parsed.questions)
    } catch {
      questions = []
    }
  }
  let out = html
  const sectionRe = /<(section|div)\b[^>]*\bdata-report-questions\b[^>]*>[\s\S]*?<\/\1>/i
  if (sectionRe.test(out)) out = out.replace(sectionRe, '')
  else if (m) out = out.replace(scriptRe, '')
  return { html: out, questions }
}

// "0 6 * * *" -> "06:00" for the <input type="time"> value.
function hourToTimeValue(hour) {
  return `${String(hour).padStart(2, '0')}:00`
}

// A friendly clock label for the schedule summary — "6:00 AM" in the
// user's locale, so the settings header reads as plain language.
function hourClockLabel(hour) {
  const d = new Date()
  d.setHours(hour, 0, 0, 0)
  return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
}

function buildModelGroups(payload) {
  if (!payload || typeof payload !== 'object') return FALLBACK_MODEL_GROUPS
  const groups = []
  for (const meta of PROVIDER_ORDER) {
    const rows = Array.isArray(payload[meta.key]) ? payload[meta.key] : null
    if (!rows || rows.length === 0) continue
    groups.push({
      key: meta.key,
      label: meta.label,
      models: rows
        .filter((row) => row && typeof row.id === 'string')
        .map((row) => ({ id: row.id, name: row.name || row.id })),
    })
  }
  return groups
}

async function fetchModelConfig(token) {
  const headers = { Authorization: `Bearer ${token}` }
  const [statusRes, modelsRes] = await Promise.all([
    fetch('/api/auth/providers/status', { headers }).catch(() => null),
    fetch('/api/auth/providers/models', { headers }).catch(() => null),
  ])
  let connected = null
  if (statusRes?.ok) {
    const data = await statusRes.json()
    connected = new Set(
      Object.entries(data || {})
        .filter(([, value]) => value && value.authenticated)
        .map(([key]) => key),
    )
  }
  const models = modelsRes?.ok ? buildModelGroups(await modelsRes.json()) : FALLBACK_MODEL_GROUPS
  return { connected, models }
}

// ---------------------------------------------------------------------------
// Date helpers — report names are <YYYY-MM-DD>.
// ---------------------------------------------------------------------------

function todayLocalDateStr() {
  const d = new Date()
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

function yesterdayLocalDateStr() {
  const d = new Date()
  d.setDate(d.getDate() - 1)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

// Relative label for a card: "Today" / "Yesterday" / full date.
function relativeLabel(dateStr) {
  if (dateStr === todayLocalDateStr()) return 'Today'
  if (dateStr === yesterdayLocalDateStr()) return 'Yesterday'
  // Anchor at noon so the date doesn't slip a day under a timezone offset.
  const d = new Date(dateStr + 'T12:00:00')
  if (Number.isNaN(d.getTime())) return dateStr
  return d.toLocaleDateString(undefined, {
    weekday: 'long', month: 'long', day: 'numeric',
  })
}

// The smaller line under the relative label — full date for Today/Yesterday,
// and the year for older cards (so a card from a different year isn't
// ambiguous).
function subLabel(dateStr) {
  const d = new Date(dateStr + 'T12:00:00')
  if (Number.isNaN(d.getTime())) return ''
  const rel = relativeLabel(dateStr)
  if (rel === 'Today' || rel === 'Yesterday') {
    return d.toLocaleDateString(undefined, { month: 'long', day: 'numeric', year: 'numeric' })
  }
  return d.toLocaleDateString(undefined, { year: 'numeric' })
}

// Weekday initial for the card's date glyph — a small calendar-ish tile that
// gives the list a visual rhythm without a heavy date component.
function weekdayInitial(dateStr) {
  const d = new Date(dateStr + 'T12:00:00')
  if (Number.isNaN(d.getTime())) return '·'
  return d.toLocaleDateString(undefined, { weekday: 'short' }).slice(0, 1)
}

function dayOfMonth(dateStr) {
  const d = new Date(dateStr + 'T12:00:00')
  if (Number.isNaN(d.getTime())) return ''
  return String(d.getDate())
}

// ---------------------------------------------------------------------------
// Theme + motion. Structural colors are CSS variables so light + dark both
// work; the dream-violet accent is the one committed hardcode. A handful of
// keyframes (drift, shimmer, rise) are injected once so loading + entrance
// states feel alive rather than static.
// ---------------------------------------------------------------------------

const ACCENT = '#7c6cf0'        // dreaming's own violet
const ACCENT_2 = '#a78bfa'      // lighter companion for gradients/glows
const ACCENT_DIM = 'rgba(124,108,240,0.13)'
const ACCENT_DIM_2 = 'rgba(167,139,250,0.10)'

const KEYFRAMES = `
@keyframes dreaming-drift {
  0%   { transform: translateY(0) rotate(0deg); opacity: .85; }
  50%  { transform: translateY(-6px) rotate(4deg); opacity: 1; }
  100% { transform: translateY(0) rotate(0deg); opacity: .85; }
}
@keyframes dreaming-shimmer {
  0%   { background-position: -180% 0; }
  100% { background-position: 180% 0; }
}
@keyframes dreaming-rise {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes dreaming-pulse {
  0%, 100% { opacity: .55; }
  50%      { opacity: 1; }
}
@keyframes dreaming-spin {
  to { transform: rotate(360deg); }
}
`

// Inject the keyframes + a couple of structural rules once. CSS-in-JS can't
// express @keyframes or :hover/:focus inline, so a single scoped <style> tag
// carries them. Idempotent — keyed by id so a remount doesn't duplicate it.
function useDreamingStyles() {
  useEffect(() => {
    const id = 'dreaming-keyframes'
    if (document.getElementById(id)) return
    const el = document.createElement('style')
    el.id = id
    el.textContent = KEYFRAMES + `
      .dreaming-card { transition: border-color .16s ease, transform .12s ease, box-shadow .16s ease, background .16s ease; }
      .dreaming-card:hover { border-color: ${ACCENT}; box-shadow: 0 6px 22px -12px ${ACCENT}; }
      .dreaming-card:active { transform: scale(.992); }
      .dreaming-card:focus-visible { outline: 2px solid ${ACCENT}; outline-offset: 2px; }
      .dreaming-pressable { transition: background .14s ease, border-color .14s ease, transform .1s ease, color .14s ease; }
      .dreaming-pressable:active { transform: scale(.97); }
      .dreaming-pressable:focus-visible { outline: 2px solid ${ACCENT}; outline-offset: 2px; }
      .dreaming-rise { animation: dreaming-rise .32s cubic-bezier(.22,.61,.36,1) both; }
      .dreaming-scroll::-webkit-scrollbar { width: 9px; height: 9px; }
      .dreaming-scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 99px; border: 2px solid transparent; background-clip: padding-box; }
      .dreaming-scroll::-webkit-scrollbar-thumb:hover { background: var(--muted); background-clip: padding-box; }
      @media (prefers-reduced-motion: reduce) {
        .dreaming-rise, [class^="dreaming-"] { animation: none !important; }
      }
    `
    document.head.appendChild(el)
    // Leave it mounted for the life of the document — other mounts reuse it.
  }, [])
}

const S = {
  root: {
    height: '100%', display: 'flex', flexDirection: 'column',
    background: 'var(--bg)', color: 'var(--text)',
    fontFamily: 'var(--font)', maxWidth: '100%', overflowX: 'hidden',
    position: 'relative',
  },
  // A faint aurora wash behind the header — pure decoration, pointer-none, so
  // the top of the app reads as a sky rather than a flat bar.
  aurora: {
    position: 'absolute', top: 0, left: 0, right: 0, height: '220px',
    background: `radial-gradient(120% 90% at 18% -10%, ${ACCENT_DIM} 0%, transparent 55%), radial-gradient(110% 80% at 92% -20%, ${ACCENT_DIM_2} 0%, transparent 60%)`,
    pointerEvents: 'none', zIndex: 0,
  },
  header: {
    padding: '22px 20px 0', display: 'flex', alignItems: 'center',
    justifyContent: 'space-between', flexShrink: 0, gap: '12px',
    flexWrap: 'wrap', position: 'relative', zIndex: 1,
  },
  titleRow: { display: 'flex', alignItems: 'center', gap: '11px', minWidth: 0 },
  moonWrap: {
    width: '34px', height: '34px', borderRadius: '11px', flexShrink: 0,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    background: `linear-gradient(135deg, ${ACCENT} 0%, ${ACCENT_2} 100%)`,
    boxShadow: `0 4px 16px -6px ${ACCENT}`,
  },
  moon: { fontSize: '18px', lineHeight: 1, animation: 'dreaming-drift 6s ease-in-out infinite' },
  titleStack: { display: 'flex', flexDirection: 'column', minWidth: 0, lineHeight: 1.15 },
  title: {
    fontSize: '21px', fontWeight: 750, letterSpacing: '-0.5px', margin: 0,
  },
  subtitle: { fontSize: '11.5px', color: 'var(--muted)', fontWeight: 500, marginTop: '1px' },
  headerRight: { display: 'flex', alignItems: 'center', gap: '9px', flexWrap: 'wrap', position: 'relative', zIndex: 1 },
  streakBadge: (quiet) => ({
    display: 'inline-flex', alignItems: 'center', gap: '5px',
    padding: '5px 11px', borderRadius: '999px',
    background: quiet ? 'var(--surface)' : ACCENT_DIM,
    color: quiet ? 'var(--muted)' : ACCENT,
    border: `1px solid ${quiet ? 'var(--border)' : 'transparent'}`,
    fontSize: '12.5px', fontWeight: 650, lineHeight: 1.2, whiteSpace: 'nowrap',
  }),
  tabs: {
    display: 'flex', gap: '2px', background: 'var(--surface)',
    borderRadius: '10px', padding: '3px', border: '1px solid var(--border)',
  },
  tab: (active) => ({
    padding: '6px 15px', borderRadius: '7px', border: 'none', cursor: 'pointer',
    fontSize: '13px', fontWeight: 650,
    background: active ? ACCENT : 'transparent',
    color: active ? '#fff' : 'var(--muted)',
    transition: 'background 0.15s, color 0.15s',
    fontFamily: 'var(--font)',
  }),
  divider: { height: '1px', background: 'var(--border)', margin: '16px 20px 0', position: 'relative', zIndex: 1 },
  scroll: {
    flex: 1, overflowY: 'auto', overflowX: 'hidden',
    padding: '16px 20px 40px',
    wordBreak: 'break-word', overflowWrap: 'anywhere', position: 'relative', zIndex: 1,
  },

  // Reports list
  list: { display: 'flex', flexDirection: 'column', gap: '11px', maxWidth: '660px', margin: '0 auto' },
  card: (latest) => ({
    display: 'flex', alignItems: 'stretch', gap: '14px',
    width: '100%', textAlign: 'left',
    border: '1px solid var(--border)', borderRadius: '16px',
    background: 'var(--surface)', padding: '15px 16px',
    cursor: 'pointer', color: 'var(--text)', fontFamily: 'var(--font)',
    position: 'relative', overflow: 'hidden',
    borderLeft: latest ? `3px solid ${ACCENT}` : '1px solid var(--border)',
  }),
  dateTile: (latest) => ({
    width: '46px', flexShrink: 0, borderRadius: '12px',
    display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
    gap: '0px', alignSelf: 'center',
    background: latest ? `linear-gradient(160deg, ${ACCENT} 0%, ${ACCENT_2} 100%)` : ACCENT_DIM,
    color: latest ? '#fff' : ACCENT,
    padding: '8px 0', lineHeight: 1,
  }),
  dateTileDay: { fontSize: '10px', fontWeight: 700, letterSpacing: '0.5px', textTransform: 'uppercase', opacity: 0.92 },
  dateTileNum: { fontSize: '19px', fontWeight: 750, letterSpacing: '-0.5px' },
  cardMain: { flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: '3px', justifyContent: 'center' },
  cardLabelRow: { display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' },
  cardLabel: { fontSize: '16px', fontWeight: 700, letterSpacing: '-0.2px', lineHeight: 1.2 },
  cardSub: { fontSize: '12px', color: 'var(--muted)', fontWeight: 500 },
  cardTldr: {
    fontSize: '13px', color: 'var(--muted)', lineHeight: 1.5, marginTop: '5px',
    display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
    overflow: 'hidden',
  },
  cardChevron: {
    alignSelf: 'center', fontSize: '20px', color: 'var(--muted)',
    flexShrink: 0, lineHeight: 1, opacity: 0.7,
  },
  latestPill: {
    fontSize: '10px', fontWeight: 750, letterSpacing: '0.7px',
    textTransform: 'uppercase', color: '#fff',
    background: ACCENT, padding: '2px 8px', borderRadius: '999px',
  },
  chatPill: {
    display: 'inline-flex', alignItems: 'center', gap: '4px',
    fontSize: '11px', fontWeight: 600, color: ACCENT,
    background: ACCENT_DIM, padding: '2px 8px', borderRadius: '999px',
  },

  empty: {
    textAlign: 'center', padding: '60px 24px 40px', color: 'var(--muted)',
    fontSize: '14px', lineHeight: 1.65, maxWidth: '440px', margin: '0 auto',
  },
  emptyMoonWrap: {
    width: '74px', height: '74px', borderRadius: '22px', margin: '0 auto 18px',
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    background: `linear-gradient(160deg, ${ACCENT_DIM} 0%, ${ACCENT_DIM_2} 100%)`,
    border: '1px solid var(--border)',
  },
  emptyMoon: { fontSize: '34px', animation: 'dreaming-drift 6s ease-in-out infinite' },
  emptyTitle: { fontSize: '17px', fontWeight: 700, color: 'var(--text)', letterSpacing: '-0.2px', marginBottom: '8px' },

  loadingWrap: { textAlign: 'center', padding: '64px 24px', color: 'var(--muted)', fontSize: '13px' },
  spinner: {
    width: '26px', height: '26px', borderRadius: '50%', margin: '0 auto 14px',
    border: `2.5px solid ${ACCENT_DIM}`, borderTopColor: ACCENT,
    animation: 'dreaming-spin 0.8s linear infinite',
  },

  errorBox: {
    maxWidth: '660px', margin: '0 auto', padding: '16px', borderRadius: '14px',
    border: '1px solid var(--border)', background: 'var(--surface)',
    color: 'var(--text)', fontSize: '13px', lineHeight: 1.55,
    display: 'flex', flexDirection: 'column', gap: '10px',
  },
  retryBtn: {
    alignSelf: 'flex-start', padding: '7px 14px', borderRadius: '9px',
    border: `1px solid ${ACCENT}`, background: 'transparent', color: ACCENT,
    fontSize: '12.5px', fontWeight: 650, cursor: 'pointer', fontFamily: 'var(--font)',
  },
  offlineBanner: {
    maxWidth: '660px', margin: '0 auto 14px', padding: '10px 14px',
    borderRadius: '12px', background: ACCENT_DIM, border: '1px solid var(--border)',
    color: 'var(--text)', fontSize: '12.5px', lineHeight: 1.45,
    display: 'flex', alignItems: 'center', gap: '8px',
  },

  // Report detail (brief + chat split view)
  detail: {
    position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
    background: 'var(--bg)', zIndex: 5,
  },
  detailBar: {
    display: 'flex', alignItems: 'center', gap: '12px',
    padding: '11px 14px', borderBottom: '1px solid var(--border)',
    flexShrink: 0, background: 'var(--surface)',
  },
  backBtn: {
    display: 'inline-flex', alignItems: 'center', gap: '5px',
    padding: '7px 13px 7px 9px', borderRadius: '10px',
    border: '1px solid var(--border)', background: 'var(--bg)',
    color: 'var(--text)', fontSize: '13px', fontWeight: 650,
    cursor: 'pointer', fontFamily: 'var(--font)', flexShrink: 0,
  },
  detailTitle: { display: 'flex', flexDirection: 'column', minWidth: 0, lineHeight: 1.25, flex: 1 },
  detailTitleMain: { fontSize: '15px', fontWeight: 700, letterSpacing: '-0.2px', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
  detailTitleSub: { fontSize: '11.5px', color: 'var(--muted)', fontWeight: 500 },

  // The split body: brief panel (top) + chat panel (bottom). On a tall
  // screen they stack and the body scrolls; the chat panel keeps a sensible
  // minimum so ChatView always has room to breathe.
  splitBody: {
    flex: 1, minHeight: 0, overflowY: 'auto', overflowX: 'hidden',
    display: 'flex', flexDirection: 'column',
  },
  briefPanel: {
    flexShrink: 0, display: 'flex', flexDirection: 'column',
    borderBottom: '1px solid var(--border)',
  },
  briefIframe: {
    width: '100%', border: 'none', background: 'var(--bg)', display: 'block',
  },
  briefLoading: {
    minHeight: '320px', display: 'flex', flexDirection: 'column',
    alignItems: 'center', justifyContent: 'center', gap: '12px',
    color: 'var(--muted)', fontSize: '13px',
  },
  chatPanel: {
    flexShrink: 0, display: 'flex', flexDirection: 'column',
    background: 'var(--bg)',
  },
  chatHeader: {
    display: 'flex', alignItems: 'center', gap: '8px',
    padding: '13px 16px 9px', flexShrink: 0,
  },
  chatHeaderDot: {
    width: '7px', height: '7px', borderRadius: '50%', background: ACCENT,
    boxShadow: `0 0 0 4px ${ACCENT_DIM}`, flexShrink: 0,
  },
  chatHeaderText: { fontSize: '13px', fontWeight: 700, letterSpacing: '-0.1px' },
  chatHeaderHint: { fontSize: '11.5px', color: 'var(--muted)', fontWeight: 500, marginLeft: 'auto' },
  chatMount: { width: '100%', flex: 1, minHeight: '420px' },
  chatResolving: {
    padding: '20px 16px 28px', display: 'flex', alignItems: 'center', gap: '10px',
    color: 'var(--muted)', fontSize: '12.5px',
  },
  noChatNote: {
    margin: '14px 16px 22px', padding: '14px 16px', borderRadius: '13px',
    background: 'var(--surface)', border: '1px dashed var(--border)',
    color: 'var(--muted)', fontSize: '12.5px', lineHeight: 1.55,
    display: 'flex', alignItems: 'flex-start', gap: '10px',
  },
  feedbackRow: {
    borderTop: '1px solid var(--border)', padding: '14px 16px 18px',
    display: 'flex', justifyContent: 'flex-end',
  },
  feedbackBtn: {
    border: '1px solid var(--border)', borderRadius: '10px', background: 'var(--surface2)',
    color: 'var(--text)', padding: '9px 12px', fontSize: '12.5px', fontWeight: 700, cursor: 'pointer',
  },

  // In-brief question cards. The agent embeds these declaratively in the
  // brief HTML (a JSON carrier inside an inert <script>); the app renders
  // them natively here so the partner taps an answer that's saved for the
  // NEXT run — never a live agent the way a background AskUserQuestion would
  // park a server-orphaned future. Shape mirrors the shell's QuestionCard.
  rqCard: {
    margin: '14px 16px 18px', padding: '16px', borderRadius: '14px',
    border: `1px solid ${ACCENT}`, background: ACCENT_DIM,
  },
  rqTitle: { fontSize: '14.5px', fontWeight: 750, color: 'var(--text)', margin: '0 0 4px' },
  rqNote: { fontSize: '12px', color: 'var(--muted)', margin: '0 0 14px', lineHeight: 1.5 },
  rqQ: (first) => ({
    marginTop: first ? 0 : '16px', paddingTop: first ? 0 : '16px',
    borderTop: first ? 'none' : '1px solid var(--border)',
  }),
  rqHeader: {
    fontSize: '11px', fontWeight: 600, textTransform: 'uppercase',
    letterSpacing: '0.5px', color: ACCENT, marginBottom: '4px',
  },
  rqText: { fontSize: '14px', marginBottom: '6px', color: 'var(--text)' },
  rqHint: { fontSize: '11px', color: 'var(--muted)', marginBottom: '8px' },
  rqOpts: { display: 'flex', flexWrap: 'wrap', gap: '6px' },
  rqOpt: (on, dim) => ({
    display: 'inline-flex', alignItems: 'center', gap: '7px',
    padding: '8px 13px', minHeight: '38px', borderRadius: '9px',
    border: `1px solid ${on ? ACCENT : 'var(--border)'}`,
    background: on ? ACCENT : 'var(--surface)',
    color: on ? 'var(--bg)' : 'var(--text)',
    opacity: dim ? 0.4 : 1,
    fontSize: '13px', cursor: 'pointer', boxSizing: 'border-box',
    fontFamily: 'var(--font)', touchAction: 'manipulation', userSelect: 'none',
  }),
  rqSubmit: (enabled) => ({
    display: 'block', width: '100%', marginTop: '14px', minHeight: '44px',
    padding: '11px', borderRadius: '11px', border: 'none',
    background: ACCENT, color: 'var(--bg)', fontSize: '14px', fontWeight: 700,
    cursor: enabled ? 'pointer' : 'default', opacity: enabled ? 1 : 0.4,
    fontFamily: 'var(--font)', touchAction: 'manipulation',
  }),
  rqDone: { marginTop: '14px', fontSize: '12.5px', color: 'var(--muted)', lineHeight: 1.5 },

  // Settings
  settingsWrap: { maxWidth: '580px', margin: '0 auto', display: 'flex', flexDirection: 'column', gap: '22px' },
  settingsCard: {
    background: 'var(--surface)', border: '1px solid var(--border)',
    borderRadius: '16px', padding: '18px', display: 'flex', flexDirection: 'column', gap: '10px',
  },
  sectionHead: { display: 'flex', alignItems: 'center', gap: '10px' },
  sectionIcon: {
    width: '30px', height: '30px', borderRadius: '9px', flexShrink: 0,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
    background: ACCENT_DIM, fontSize: '15px',
  },
  sectionLabel: { fontSize: '14.5px', fontWeight: 700, letterSpacing: '-0.1px', margin: 0 },
  note: { fontSize: '12.5px', color: 'var(--muted)', margin: 0, lineHeight: 1.55 },
  timeRow: { display: 'flex', alignItems: 'center', gap: '10px', flexWrap: 'wrap', marginTop: '2px' },
  timeInput: {
    padding: '9px 12px', fontSize: '16px', fontFamily: 'var(--font)', fontWeight: 600,
    background: 'var(--bg)', color: 'var(--text)',
    border: '1px solid var(--border)', borderRadius: '10px',
    outline: 'none', width: '132px',
  },
  customCronNote: {
    fontSize: '12px', color: 'var(--muted)', lineHeight: 1.5,
    padding: '10px 12px', borderRadius: '11px',
    background: 'var(--bg)', border: '1px solid var(--border)', marginTop: '2px',
  },
  agentSelect: {
    width: '100%', minHeight: '42px', padding: '9px 12px',
    border: '1px solid var(--border)', borderRadius: '10px',
    background: 'var(--bg)', color: 'var(--text)', fontSize: '14px',
    fontFamily: 'var(--font)', fontWeight: 650, outline: 'none',
  },
  agentMeta: {
    fontSize: '12px', color: 'var(--muted)', lineHeight: 1.5,
    padding: '10px 12px', borderRadius: '11px',
    background: 'var(--bg)', border: '1px solid var(--border)',
  },
  modelLabel: {
    fontSize: '11px', color: 'var(--muted)', fontWeight: 750,
    textTransform: 'uppercase', letterSpacing: '0.4px', marginTop: '4px',
  },
  verbList: { display: 'flex', flexDirection: 'column', gap: '8px', marginTop: '2px' },
  verbRow: (on) => ({
    display: 'flex', alignItems: 'flex-start', gap: '11px',
    padding: '12px 13px', borderRadius: '12px', cursor: 'pointer',
    background: on ? ACCENT_DIM : 'var(--bg)',
    border: `1px solid ${on ? ACCENT : 'var(--border)'}`,
    userSelect: 'none', transition: 'background 0.14s, border-color 0.14s',
  }),
  verbRadio: (on) => ({
    width: '17px', height: '17px', borderRadius: '999px', marginTop: '1px',
    border: `2px solid ${on ? ACCENT : 'var(--muted)'}`,
    background: 'transparent', flexShrink: 0, position: 'relative',
    boxShadow: on ? `inset 0 0 0 3.5px ${ACCENT}` : 'none',
    transition: 'box-shadow .14s, border-color .14s',
  }),
  verbMain: { display: 'flex', flexDirection: 'column', gap: '2px', minWidth: 0 },
  verbTitle: { fontSize: '13.5px', fontWeight: 650 },
  verbHint: { fontSize: '12px', color: 'var(--muted)', lineHeight: 1.45 },
  saveRow: { display: 'flex', alignItems: 'center', gap: '12px', flexWrap: 'wrap', marginTop: '2px' },
  saveBtn: (busy) => ({
    padding: '10px 22px', borderRadius: '12px', border: 'none',
    background: busy ? 'var(--surface)' : ACCENT,
    color: busy ? 'var(--muted)' : '#fff',
    fontSize: '13.5px', fontWeight: 700, cursor: busy ? 'default' : 'pointer',
    fontFamily: 'var(--font)', transition: 'background 0.15s, opacity .15s',
    boxShadow: busy ? 'none' : `0 6px 18px -8px ${ACCENT}`,
  }),
  toast: { fontSize: '12.5px', color: 'var(--green, #3fb950)', fontWeight: 650 },
  errorToast: { fontSize: '12.5px', color: 'var(--danger, #f85149)', fontWeight: 650 },
  scheduleHint: {
    fontSize: '12px', color: 'var(--muted)', lineHeight: 1.55,
    padding: '11px 13px', borderRadius: '12px',
    background: ACCENT_DIM, border: '1px solid var(--border)',
    display: 'flex', alignItems: 'flex-start', gap: '8px',
  },
}

// ---------------------------------------------------------------------------
// Storage — raw fetch with the app token (per the data contract). JSON paths
// parse JSON; report bodies are read as text. A real 404 (storage empty on
// first run) is normal and returns the `notFound` shape so callers can tell
// it apart from a network failure (`error`) and treat each correctly.
// ---------------------------------------------------------------------------

function makeStorage(appId, token) {
  const ms = (typeof window !== 'undefined' && window.mobius && window.mobius.storage) || null
  const headers = { Authorization: `Bearer ${token}` }
  const base = `/api/storage/apps/${appId}`
  const listBase = `/api/storage/apps-list/${appId}`

  async function getJSON(path) {
    try {
      if (ms && typeof ms.get === 'function') {
        const data = await ms.get(path)
        return data == null ? { notFound: true } : { data }
      }
      const r = await fetch(`${base}/${path}`, { headers })
      if (r.status === 404) return { notFound: true }
      if (!r.ok) return { error: r.status }
      return { data: await r.json() }
    } catch {
      return { error: 0 }
    }
  }

  async function putJSON(path, obj) {
    if (ms && typeof ms.set === 'function') {
      await ms.set(path, obj)
      return true
    }
    const r = await fetch(`${base}/${path}`, {
      method: 'PUT',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(obj),
    })
    if (!r.ok) throw new Error(`PUT ${path} failed (${r.status})`)
    return true
  }

  async function getReportHtml(name) {
    // Report bodies are raw HTML documents — read as text, not JSON.
    try {
      const r = await fetch(`${base}/reports/${name}`, { headers })
      if (r.status === 404) return { notFound: true }
      if (!r.ok) return { error: r.status }
      return { data: await r.text() }
    } catch {
      return { error: 0 }
    }
  }

  // Resolve the morning chat for a report date. The cron SHOULD write a
  // sibling `reports/<date>.meta.json` = { "chat_id": "<id>" } when it opens
  // the morning chat; this reads it (with the app token, same as every other
  // read). Returns the chat_id string, or null when there's no meta yet (the
  // brief then stands alone). A network error is swallowed to null too — the
  // brief is still readable; the chat just doesn't mount this open.
  async function getReportChatId(dateStr) {
    try {
      const r = await fetch(`${base}/reports/${dateStr}.meta.json`, { headers })
      if (!r.ok) return null
      const data = await r.json()
      const id = data && (data.chat_id ?? data.chatId ?? data.morning_chat)
      return typeof id === 'string' && id.trim() ? id.trim() : null
    } catch {
      return null
    }
  }

  // Enumerate reports via the listing endpoint (newest-first), walking the
  // cursor. A non-advancing cursor is treated as a server fault rather than
  // spinning forever. Returns { dates: [...] } on success, or { error } /
  // { notFound } so the caller can distinguish "no reports yet" from "the
  // listing call failed" and keep its cached snapshot in the latter case.
  async function listReportDates() {
    const out = []
    let cursor = null
    try {
      for (let guard = 0; guard < 50; guard++) {
        const url = `${listBase}/reports/`
          + (cursor ? `?cursor=${encodeURIComponent(cursor)}` : '')
        const r = await fetch(url, { headers })
        if (r.status === 404) return { dates: [] } // dir not created yet = empty
        if (!r.ok) return { error: r.status }
        const data = await r.json()
        for (const e of data.entries || []) {
          if (e.type === 'file' && typeof e.name === 'string' && e.name.endsWith('.html')) {
            out.push(e.name.slice(0, -'.html'.length))
          }
        }
        const prev = cursor
        cursor = data.next_cursor
        if (!cursor) break
        if (cursor === prev) return { error: -1 } // server returned same page
      }
    } catch {
      return { error: 0 }
    }
    // ISO date names sort lexicographically = chronologically; newest first.
    out.sort((a, b) => (a < b ? 1 : a > b ? -1 : 0))
    return { dates: out }
  }

  return { getJSON, putJSON, getReportHtml, getReportChatId, listReportDates }
}

// ---------------------------------------------------------------------------
// Tiny offline snapshot. Dreaming reads via direct fetch (text bodies +
// apps-list), neither of which the platform read-cache covers, so we keep our
// own localStorage snapshot: the dates list, the streak, and the latest
// summary. This is read-only mirror state — only the cron writes reports, so
// the snapshot just paints the same thing the user saw before they lost
// connectivity. Bodies aren't cached here (the report iframe re-fetches; if
// offline it shows a graceful error), only the cheap list metadata.
// ---------------------------------------------------------------------------

const CACHE_VERSION = 1
function cacheKey(appId) { return `dreaming:${appId}:list:v${CACHE_VERSION}` }

function readCache(appId) {
  try {
    const raw = localStorage.getItem(cacheKey(appId))
    if (!raw) return null
    const p = JSON.parse(raw)
    if (!p || typeof p !== 'object') return null
    return {
      dates: Array.isArray(p.dates) ? p.dates.filter((d) => typeof d === 'string') : [],
      streak: Number.isFinite(p.streak) ? p.streak : 0,
      lastSummary: typeof p.lastSummary === 'string' ? p.lastSummary : '',
      lastRun: typeof p.lastRun === 'string' ? p.lastRun : '',
    }
  } catch {
    return null
  }
}

function writeCache(appId, snap) {
  try {
    localStorage.setItem(cacheKey(appId), JSON.stringify(snap))
  } catch {
    // Quota / private-mode Safari: skip silently. In-memory state still works.
  }
}

// ---------------------------------------------------------------------------
// Online/offline hook — runtime signal if present, else navigator.onLine.
// ---------------------------------------------------------------------------

function useOnline() {
  const initial = (() => {
    if (typeof window === 'undefined') return true
    if (typeof window.mobius?.online === 'boolean') return window.mobius.online
    return navigator.onLine !== false
  })()
  const [online, setOnline] = useState(initial)
  useEffect(() => {
    if (typeof window === 'undefined') return undefined
    const up = () => setOnline(true)
    const down = () => setOnline(false)
    window.addEventListener('online', up)
    window.addEventListener('offline', down)
    let unsub = null
    if (window.mobius && typeof window.mobius.onChange === 'function') {
      unsub = window.mobius.onChange((s) => {
        if (typeof s?.online === 'boolean') setOnline(s.online)
      })
    }
    return () => {
      window.removeEventListener('online', up)
      window.removeEventListener('offline', down)
      if (unsub) unsub()
    }
  }, [])
  return online
}

// ---------------------------------------------------------------------------
// Morning chat embed. `window.mobius.chat({ mount, chatId })` mounts the real
// ChatView (composer + live SSE + tappable AskUserQuestion cards) inside a
// nested same-origin iframe that runs in the SHELL origin — so it carries the
// owner JWT and can read/post the morning chat the nightly run opened. The
// chat is app-attributed (created via /api/app-chats), so the drawer's chat
// list hides it and THIS embed is its home surface; the owner JWT still
// drives it (the owner can always read/post an app's chats), and legacy
// owner-created morning chats render through the same path.
//
// We MUST pass an existing chatId. The runtime can lazy-create a chat when
// none is given, but that would make a brand-new empty chat, not find the
// morning one — so a null chatId here renders nothing (the caller shows its
// own "no chat" note). The handle is torn down on unmount / date change so we
// never leak the nested iframe.
// ---------------------------------------------------------------------------

function MorningChat({ chatId }) {
  const mountRef = useRef(null)
  const [phase, setPhase] = useState('mounting') // mounting | live | unavailable

  useEffect(() => {
    const mount = mountRef.current
    if (!mount || !chatId) { setPhase('unavailable'); return undefined }
    if (!window.mobius || typeof window.mobius.chat !== 'function') {
      // Running outside the shell embed (e.g. standalone) — no chat bridge.
      setPhase('unavailable')
      return undefined
    }
    let handle = null
    let cancelled = false
    setPhase('mounting')
    Promise.resolve(window.mobius.chat({ mount, chatId }))
      .then((h) => {
        if (cancelled) { try { h && h.destroy && h.destroy() } catch {} return }
        handle = h
        setPhase('live')
      })
      .catch(() => { if (!cancelled) setPhase('unavailable') })
    return () => {
      cancelled = true
      try { handle && handle.destroy && handle.destroy() } catch {}
      // Belt-and-suspenders: the runtime appends one iframe to `mount`; clear
      // any leftover node so a fast date switch can't stack two embeds.
      if (mount) { try { mount.replaceChildren() } catch {} }
    }
  }, [chatId])

  if (phase === 'unavailable') {
    return (
      <div style={S.noChatNote}>
        <span aria-hidden="true" style={{ fontSize: '15px', lineHeight: 1.2 }}>💬</span>
        <span>
          The conversation about this brief isn’t available in this view.
          Open the Dreaming app inside Möbius to reply.
        </span>
      </div>
    )
  }

  return (
    <div style={{ position: 'relative' }}>
      {phase === 'mounting' && (
        <div style={S.chatResolving}>
          <span style={{ ...S.spinner, width: '16px', height: '16px', margin: 0, borderWidth: '2px' }} aria-hidden="true" />
          Opening the conversation…
        </div>
      )}
      <div ref={mountRef} style={{ ...S.chatMount, display: phase === 'live' ? 'block' : 'none' }} />
    </div>
  )
}

function FeedbackLauncher({ dateStr, chatId }) {
  const openFeedbackChat = () => {
    // Keep the draft to a one-line header + the partner's entry. The brief
    // itself is already on screen (and, when we continue the morning chat,
    // in the conversation), so echoing an excerpt back is just noise.
    const draft = [
      `Feedback on the Dreaming brief for ${dateStr}:`,
      '',
      'My feedback:',
    ].join('\n')
    // Prefer continuing the morning chat the nightly run opened — it already
    // holds the brief and the agent's overnight work, so the partner lands
    // in-context and can inspect it before replying. Fall back to a fresh
    // chat only when no morning chat was linked for this date.
    window.parent.postMessage(
      chatId
        ? { type: 'moebius:open-chat', chatId, draft }
        : { type: 'moebius:new-chat', draft },
      window.location.origin,
    )
  }
  return (
    <div style={S.feedbackRow}>
      <button style={S.feedbackBtn} className="dreaming-pressable" onClick={openFeedbackChat}>
        Give feedback on this brief
      </button>
    </div>
  )
}

// Native tap-card UI for the brief's in-report questions. Mirrors the shell
// QuestionCard's shape ({question, header, multiSelect, options:[{label,
// description}]}) but is a single-file, install-safe copy — no sibling
// imports, no streaming/answeredMap plumbing. Collects an answer per
// question; on submit calls onAnswer({ "<question text>": "<chosen
// label(s)>" }) and flips to a local "answered" state. The caller persists
// the answers for the NEXT run (not a live agent) — the note copy says so.
function ReportQuestions({ questions, onAnswer }) {
  const [picks, setPicks] = useState({})
  const [answered, setAnswered] = useState(false)

  if (!Array.isArray(questions) || questions.length === 0) return null

  const allAnswered = questions.every((q) => {
    const p = picks[q.question]
    return q.multiSelect ? Array.isArray(p) && p.length > 0 : !!p
  })

  const choose = (q, label) => {
    if (answered) return
    setPicks((prev) => {
      if (q.multiSelect) {
        const cur = Array.isArray(prev[q.question]) ? prev[q.question] : []
        const next = cur.includes(label) ? cur.filter((l) => l !== label) : [...cur, label]
        return { ...prev, [q.question]: next }
      }
      return { ...prev, [q.question]: label }
    })
  }

  const submit = () => {
    if (!allAnswered || answered) return
    const answers = {}
    for (const q of questions) {
      const p = picks[q.question]
      answers[q.question] = Array.isArray(p) ? p.join(', ') : (p || '')
    }
    setAnswered(true)
    onAnswer?.(answers)
  }

  return (
    <div style={S.rqCard}>
      <p style={S.rqTitle}>A few questions for tomorrow night</p>
      <p style={S.rqNote}>
        Your answers guide my next run — they won’t change this brief.
      </p>
      {questions.map((q, qi) => {
        const isMulti = q.multiSelect
        const cur = picks[q.question]
        const selected = (label) =>
          isMulti ? (Array.isArray(cur) && cur.includes(label)) : cur === label
        return (
          <div key={qi} style={S.rqQ(qi === 0)}>
            {q.header && <div style={S.rqHeader}>{q.header}</div>}
            <div style={S.rqText}>{q.question}</div>
            {!answered && (
              <div style={S.rqHint}>{isMulti ? 'Select all that apply' : 'Choose one'}</div>
            )}
            <div
              style={S.rqOpts}
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
                    className="dreaming-pressable"
                    style={S.rqOpt(on, dim)}
                    onClick={answered ? undefined : () => choose(q, opt.label)}
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
        <div style={S.rqDone}>Saved — I’ll use this for tomorrow night’s dream.</div>
      ) : (
        <button
          type="button"
          className="dreaming-pressable"
          style={S.rqSubmit(allAnswered)}
          onClick={submit}
          disabled={!allAnswered}
        >
          Save for next time
        </button>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Report detail — the brief + chat split view.
//
// The brief is the static HTML the agent authored: rendered in a sandboxed
// srcDoc iframe. Sandbox: allow-scripts WITHOUT allow-same-origin — the
// iframe has a null origin so its scripts cannot access the parent's DOM,
// localStorage, or owner JWT (the security risk of allow-same-origin+scripts).
// hardenReportHtml injects a tiny height-reporter script that postMessages
// scrollHeight to the parent. The parent sizes the iframe from those messages
// so the page scrolls as one column (brief, then chat) without nesting two
// scroll regions. Beneath it, the morning chat embed.
// ---------------------------------------------------------------------------

function ReportDetail({ dateStr, storage, online, onBack }) {
  const [state, setState] = useState({ phase: 'loading', html: '' })
  // The agent's in-report questions, extracted from the raw brief HTML and
  // rendered as native tap cards below the iframe. The carrier is stripped
  // from the HTML before hardenReportHtml so it never reaches the iframe.
  const [questions, setQuestions] = useState([])
  const [chatId, setChatId] = useState(undefined) // undefined=resolving, null=none, string=id
  const [briefHeight, setBriefHeight] = useState(360)
  const iframeRef = useRef(null)

  // Load the brief body + resolve its chat in parallel.
  useEffect(() => {
    let cancelled = false
    setState({ phase: 'loading', html: '' })
    setQuestions([])
    setChatId(undefined)
    setBriefHeight(360)
    ;(async () => {
      const res = await storage.getReportHtml(`${dateStr}.html`)
      if (cancelled) return
      if (res.data != null) {
        // Extract the question carrier from the RAW HTML before hardening —
        // hardenReportHtml/the sandbox would otherwise carry an inert script
        // into the iframe (and the partner can't answer there). Strip it,
        // harden the remainder, render the questions natively below.
        const { html: cleaned, questions: qs } = extractReportQuestions(res.data)
        setQuestions(qs)
        setState({ phase: 'ready', html: hardenReportHtml(cleaned) })
      }
      else if (res.notFound) setState({ phase: 'missing', html: '' })
      else setState({ phase: 'error', html: '' })
    })()
    ;(async () => {
      const id = await storage.getReportChatId(dateStr)
      if (!cancelled) setChatId(id)
    })()
    return () => { cancelled = true }
  }, [dateStr, storage])

  // Size the brief iframe from postMessage events sent by the injected
  // height-reporter script (see hardenReportHtml + REPORT_HEIGHT_SCRIPT).
  // The iframe runs with allow-scripts but WITHOUT allow-same-origin, so
  // contentDocument is NOT readable from the parent — we receive height
  // passively via postMessage instead.
  useEffect(() => {
    const onMessage = (ev) => {
      if (!ev.data || ev.data.type !== 'dreaming:brief-height') return
      const h = Number(ev.data.height)
      if (Number.isFinite(h) && h > 0) {
        setBriefHeight(Math.min(Math.max(h, 200), 100000))
      }
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [])

  const onIframeLoad = useCallback(() => {
    // The height reporter inside the iframe fires on DOMContentLoaded and
    // on ResizeObserver changes. Nothing to do here from the parent side,
    // but we keep the onLoad prop in case subclasses need it later.
  }, [])

  return (
    <div style={S.detail} className="dreaming-rise">
      <div style={S.detailBar}>
        <button
          style={S.backBtn} className="dreaming-pressable"
          onClick={onBack} aria-label="Back to reports"
        >
          <span aria-hidden="true" style={{ fontSize: '16px' }}>‹</span> Briefs
        </button>
        <div style={S.detailTitle}>
          <span style={S.detailTitleMain}>{relativeLabel(dateStr)}’s brief</span>
          <span style={S.detailTitleSub}>{subLabel(dateStr)}</span>
        </div>
      </div>

      {/* Feedback launcher sits at the TOP of the brief now — the open-ended
          escape hatch is reachable without scrolling the whole read. It stays
          gated on chatId (undefined while the meta read resolves) so a fast
          tap can't open a blank new chat instead of the morning one. */}
      {state.phase === 'ready' && chatId !== undefined && (
        <FeedbackLauncher dateStr={dateStr} chatId={chatId} />
      )}

      {state.phase === 'loading' && (
        <div style={S.briefLoading}>
          <span style={S.spinner} aria-hidden="true" />
          <span>Opening your brief…</span>
        </div>
      )}

      {state.phase === 'missing' && (
        <div style={{ ...S.empty, paddingTop: '56px' }}>
          This brief is no longer available.
        </div>
      )}

      {state.phase === 'error' && (
        <div style={{ ...S.empty, paddingTop: '56px' }}>
          {online
            ? 'This brief could not be loaded. Try opening it again in a moment.'
            : 'You’re offline — open this brief again once you’re back online.'}
        </div>
      )}

      {state.phase === 'ready' && (
        <div style={S.splitBody} className="dreaming-scroll">
          <div style={S.briefPanel}>
            <iframe
              ref={iframeRef}
              style={{ ...S.briefIframe, height: `${briefHeight}px` }}
              title={`Morning brief for ${dateStr}`}
              srcDoc={state.html}
              onLoad={onIframeLoad}
              // allow-scripts lets the injected height-reporter run.
              // allow-same-origin is intentionally absent: without it the
              // iframe gets a null origin, so its scripts cannot reach the
              // parent's DOM, localStorage, or owner JWT regardless of what
              // the brief HTML contains. allow-popups lets the agent include
              // external links that open in a new tab.
              sandbox="allow-scripts allow-popups allow-popups-to-escape-sandbox"
            />
          </div>

          {/* In-brief question cards render BETWEEN the read and the morning
              chat. The carrier was extracted from the raw HTML and stripped
              before srcDoc, so these taps are the interactive surface. Answers
              persist to question-answers/<date>.json for the NEXT run — no
              live agent waits (a background AskUserQuestion would park a future
              a server reset orphans). */}
          {questions.length > 0 && (
            <ReportQuestions
              questions={questions}
              onAnswer={(answers) => {
                const body = {
                  report_date: dateStr,
                  answered_at: new Date().toISOString(),
                  answers,
                  questions,
                }
                // Bare object on a .json path → stored as-is (no envelope).
                // putJSON routes through the offline runtime, so a tap made
                // offline queues and drains on reconnect.
                Promise.resolve(storage.putJSON(`question-answers/${dateStr}.json`, body)).catch(() => {})
              }}
            />
          )}

          <div style={S.chatPanel}>
            <div style={S.chatHeader}>
              <span style={S.chatHeaderDot} aria-hidden="true" />
              <span style={S.chatHeaderText}>Morning conversation</span>
              {chatId && <span style={S.chatHeaderHint}>reply below</span>}
            </div>
            {chatId === undefined ? (
              <div style={S.chatResolving}>
                <span style={{ ...S.spinner, width: '16px', height: '16px', margin: 0, borderWidth: '2px' }} aria-hidden="true" />
                Finding the conversation…
              </div>
            ) : chatId === null ? (
              <div style={S.noChatNote}>
                <span aria-hidden="true" style={{ fontSize: '15px', lineHeight: 1.2 }}>🌙</span>
                <span>
                  No conversation was opened for this brief — it’s a read-only
                  morning note. Any questions appear as tap cards in the brief
                  above; open Dreaming inside Möbius to reply here.
                </span>
              </div>
            ) : (
              <MorningChat chatId={chatId} />
            )}
          </div>
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Reports list
// ---------------------------------------------------------------------------

function ReportsList({ appId, storage, online, onOpen }) {
  const cached = useMemo(() => readCache(appId), [appId])
  const [dates, setDates] = useState(cached?.dates || [])
  const [streak, setStreak] = useState(cached?.streak || 0)
  const [lastSummary, setLastSummary] = useState(cached?.lastSummary || '')
  const [phase, setPhase] = useState('loading') // loading | ready | error
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    let cancelled = false
    ;(async () => {
      setPhase((p) => (dates.length ? p : 'loading'))
      const [listRes, stateRes] = await Promise.all([
        storage.listReportDates(),
        storage.getJSON('state.json'),
      ])
      if (cancelled) return

      // state.json: 404 is normal (cron hasn't written it) -> streak 0.
      let nextStreak = 0
      let nextSummary = ''
      let nextLastRun = ''
      if (stateRes.data && typeof stateRes.data === 'object') {
        nextStreak = Number.isFinite(stateRes.data.streak) ? stateRes.data.streak : 0
        nextSummary = typeof stateRes.data.last_summary === 'string' ? stateRes.data.last_summary : ''
        nextLastRun = typeof stateRes.data.last_run === 'string' ? stateRes.data.last_run : ''
      } else if (stateRes.error != null && cached) {
        // Couldn't reach state.json (offline) — keep the cached header.
        nextStreak = cached.streak
        nextSummary = cached.lastSummary
      }
      setStreak(nextStreak)
      setLastSummary(nextSummary)

      if (listRes.dates) {
        // Listing succeeded (even if empty []): trust the server.
        setDates(listRes.dates)
        setPhase('ready')
        writeCache(appId, {
          dates: listRes.dates, streak: nextStreak,
          lastSummary: nextSummary, lastRun: nextLastRun,
        })
      } else if (cached && cached.dates.length) {
        // Listing failed but we have a cached snapshot — show it.
        setDates(cached.dates)
        setPhase('ready')
      } else {
        // Listing failed and nothing cached — surface a retryable error.
        setPhase('error')
      }
    })()
    return () => { cancelled = true }
    // reloadKey forces a manual retry; dates intentionally excluded so a
    // state update inside the effect doesn't re-trigger it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [appId, storage, reloadKey])

  if (phase === 'loading' && dates.length === 0) {
    return (
      <div style={S.loadingWrap}>
        <span style={S.spinner} aria-hidden="true" />
        <div>Gathering last night’s dream…</div>
      </div>
    )
  }

  if (phase === 'error' && dates.length === 0) {
    return (
      <div style={S.errorBox}>
        <span>
          {online
            ? 'Couldn’t load your briefs just now.'
            : 'You’re offline and there’s nothing cached yet.'}
        </span>
        {online && (
          <button style={S.retryBtn} className="dreaming-pressable" onClick={() => setReloadKey((k) => k + 1)}>
            Try again
          </button>
        )}
      </div>
    )
  }

  if (dates.length === 0) {
    return (
      <div style={S.empty}>
        <div style={S.emptyMoonWrap}>
          <span style={S.emptyMoon} aria-hidden="true">🌙</span>
        </div>
        <div style={S.emptyTitle}>No briefs yet</div>
        Dreaming runs overnight — consolidating what the day’s agents learned,
        tidying your Mind, and tending your apps. Your first morning brief will
        be waiting right here.
      </div>
    )
  }

  return (
    <div className="dreaming-rise">
      <StreakBar streak={streak} />
      {!online && (
        <div style={S.offlineBanner}>
          <span aria-hidden="true">🌙</span>
          Offline — showing your last cached briefs. Tonight’s dream appears
          once you’re back online.
        </div>
      )}
      <div style={S.list}>
        {dates.map((d, i) => (
          <button
            key={d}
            style={S.card(i === 0)}
            className="dreaming-card"
            onClick={() => onOpen(d)}
          >
            <div style={S.dateTile(i === 0)} aria-hidden="true">
              <span style={S.dateTileDay}>{weekdayInitial(d)}</span>
              <span style={S.dateTileNum}>{dayOfMonth(d)}</span>
            </div>
            <div style={S.cardMain}>
              <div style={S.cardLabelRow}>
                <span style={S.cardLabel}>{relativeLabel(d)}</span>
                {i === 0 && <span style={S.latestPill}>Latest</span>}
              </div>
              <span style={S.cardSub}>{subLabel(d)}</span>
              {i === 0 && lastSummary && (
                <span style={S.cardTldr}>{lastSummary}</span>
              )}
            </div>
            <span style={S.cardChevron} aria-hidden="true">›</span>
          </button>
        ))}
      </div>
    </div>
  )
}

function StreakBar({ streak }) {
  if (!streak || streak < 1) return null
  const flames = Math.min(streak, 5)
  return (
    <div style={{ maxWidth: '660px', margin: '0 auto 16px', display: 'flex' }}>
      <span style={{ ...S.streakBadge(false), padding: '7px 13px', fontSize: '13px' }}>
        <span aria-hidden="true" style={{ animation: 'dreaming-drift 4s ease-in-out infinite' }}>🔥</span>
        <strong style={{ fontWeight: 750 }}>{streak}</strong>
        <span style={{ fontWeight: 550 }}>
          {streak === 1 ? 'morning in a row' : 'mornings in a row'}
        </span>
        <span aria-hidden="true" style={{ marginLeft: '4px', letterSpacing: '1px', opacity: 0.55, fontSize: '9px' }}>
          {'•'.repeat(flames)}
        </span>
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

function SettingsTab({ appId, storage, online, token }) {
  const [hour, setHour] = useState(DEFAULT_HOUR)
  const [excludeApps, setExcludeApps] = useState([])
  const [settingsExtra, setSettingsExtra] = useState({})
  const [provider, setProvider] = useState(DEFAULT_PROVIDER)
  const [model, setModel] = useState(DEFAULT_MODEL)
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
      })
      setToast('Saved ✓')
      setTimeout(() => setToast(''), 2600)
    } catch {
      setError(online ? 'Could not save — try again.' : 'You’re offline — reconnect to save.')
    } finally {
      setSaving(false)
    }
  }, [saving, cronIsCustom, rawCron, hour, excludeApps, provider, model, settingsExtra, storage, online])

  if (loading) {
    return (
      <div style={S.loadingWrap}>
        <span style={S.spinner} aria-hidden="true" />
        <div>Loading settings…</div>
      </div>
    )
  }

  return (
    <div style={S.settingsWrap} className="dreaming-rise">
      <div style={S.settingsCard}>
        <div style={S.sectionHead}>
          <span style={S.sectionIcon} aria-hidden="true">⏰</span>
          <h2 style={S.sectionLabel}>When to dream</h2>
        </div>
        <p style={S.note}>
          Pick the hour your morning brief should be ready. Dreaming writes it
          overnight so it’s waiting when you wake.
        </p>
        {cronIsCustom ? (
          <div style={S.customCronNote}>
            You have a custom schedule set (<code>{rawCron}</code>). Pick an
            hour below to switch to a simple daily time, or leave it as-is.
            <div style={{ ...S.timeRow, marginTop: '10px' }}>
              <input
                type="time"
                step="3600"
                style={S.timeInput}
                value={hourToTimeValue(hour)}
                onChange={onTimeChange}
                aria-label="Daily brief time"
              />
              <span style={S.note}>on the hour, every day</span>
            </div>
          </div>
        ) : (
          <div style={S.timeRow}>
            <input
              type="time"
              step="3600"
              style={S.timeInput}
              value={hourToTimeValue(hour)}
              onChange={onTimeChange}
              aria-label="Daily brief time"
            />
            <span style={S.note}>
              ready around <strong style={{ color: 'var(--text)', fontWeight: 650 }}>{hourClockLabel(hour)}</strong>, every day
            </span>
          </div>
        )}
        <div style={S.scheduleHint}>
          <span aria-hidden="true">💡</span>
          <span>
            Schedule changes take effect after the dreaming agent re-installs
            its overnight job — usually by the next run. The app saves your
            preference; the agent picks it up from there.
          </span>
        </div>
      </div>

      <div style={S.settingsCard}>
        <div style={S.sectionHead}>
          <span style={S.sectionIcon} aria-hidden="true">🤖</span>
          <h2 style={S.sectionLabel}>Nightly model</h2>
        </div>
        <p style={S.note}>
          The model Dreaming uses for the overnight pass. It runs its own
          procedure with the default skill.
        </p>
        {modelGroups === null ? (
          <div style={S.note}>Loading models…</div>
        ) : modelGroups.length === 0 ? (
          // Models API unavailable — fall back to letting the CLI choose.
          <div style={S.note}>
            Model list unavailable. Dreaming will use the CLI's default model
            for your account.
          </div>
        ) : (
          <>
            <select
              style={S.agentSelect}
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
              aria-label="Dreaming model"
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
                          {m.name} ({m.id})
                        </option>
                      )
                    })}
                  </optgroup>
                )
              })}
            </select>
            <div style={S.agentMeta}>
              {(modelGroups.find((group) => group.key === provider)?.label || provider)}
              {' · '}
              {model || 'provider default'}
            </div>
          </>
        )}
      </div>

      <div style={S.saveRow}>
        <button style={S.saveBtn(saving)} className="dreaming-pressable" onClick={save} disabled={saving}>
          {saving ? 'Saving…' : 'Save settings'}
        </button>
        {toast && <span style={S.toast}>{toast}</span>}
        {error && <span style={S.errorToast}>{error}</span>}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

export default function App({ appId, token }) {
  useDreamingStyles()
  const [tab, setTab] = useState('reports')
  const [openDate, setOpenDate] = useState(null)
  const online = useOnline()
  const storage = useMemo(() => makeStorage(appId, token), [appId, token])
  const detailNavRef = useRef(null)

  // Surface the streak in the header on the reports tab. We read it once
  // here (cheap, cached) so the badge is present even before the list
  // finishes its own load. The list keeps its own authoritative copy.
  const [headerStreak, setHeaderStreak] = useState(() => readCache(appId)?.streak || 0)
  useEffect(() => {
    let cancelled = false
    ;(async () => {
      const res = await storage.getJSON('state.json')
      if (cancelled) return
      if (res.data && Number.isFinite(res.data.streak)) {
        setHeaderStreak(res.data.streak)
      }
    })()
    return () => { cancelled = true }
  }, [storage, appId])

  const closeDetail = useCallback(() => {
    try { detailNavRef.current?.close?.() } catch {}
    detailNavRef.current = null
    setOpenDate(null)
  }, [])

  const openDetail = useCallback(async (dateStr) => {
    try { detailNavRef.current?.close?.() } catch {}
    detailNavRef.current = null
    if (window.mobius?.nav?.open) {
      const handle = window.mobius.nav.open('dreaming-report', () => {
        detailNavRef.current = null
        setOpenDate(null)
      })
      detailNavRef.current = handle
      await handle.ready?.catch(() => false)
      if (detailNavRef.current !== handle) return
    }
    setOpenDate(dateStr)
  }, [])

  useEffect(() => () => {
    try { detailNavRef.current?.close?.() } catch {}
  }, [])

  return (
    <div style={S.root}>
      <div style={S.aurora} aria-hidden="true" />
      <div style={S.header}>
        <div style={S.titleRow}>
          <span style={S.moonWrap} aria-hidden="true">
            <span style={S.moon}>🌙</span>
          </span>
          <div style={S.titleStack}>
            <h1 style={S.title}>Dreaming</h1>
            <span style={S.subtitle}>your overnight brief</span>
          </div>
        </div>
        <div style={S.headerRight}>
          {headerStreak >= 1 && (
            <span style={S.streakBadge(false)} title={`${headerStreak} mornings in a row`}>
              <span aria-hidden="true">🔥</span>
              {headerStreak}
            </span>
          )}
          <div style={S.tabs}>
            <button style={S.tab(tab === 'reports')} onClick={() => setTab('reports')}>
              Briefs
            </button>
            <button style={S.tab(tab === 'settings')} onClick={() => { closeDetail(); setTab('settings') }}>
              Settings
            </button>
          </div>
        </div>
      </div>
      <div style={S.divider} />
      <div style={{ ...S.scroll }} className="dreaming-scroll">
        {tab === 'reports' ? (
          <>
            <ReportsList
              appId={appId}
              storage={storage}
              online={online}
              onOpen={openDetail}
            />
            {openDate && (
              <ReportDetail
                dateStr={openDate}
                storage={storage}
                online={online}
                onBack={closeDetail}
              />
            )}
          </>
        ) : (
          <SettingsTab appId={appId} storage={storage} online={online} token={token} />
        )}
      </div>
    </div>
  )
}
