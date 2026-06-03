/* Dreaming — the nightly morning-brief viewer.
 *
 * Lists the dated reports the dreaming agent leaves overnight, tracks a
 * streak, and lets the owner set the run hour + verbosity. Opening a brief
 * shows TWO things stacked: the brief HTML up top (a sandboxed, script-free
 * iframe — the agent's static page) and, beneath it, the MORNING CHAT the
 * nightly run opened — the conversation about that brief, live, with a real
 * composer and tappable AskUserQuestion cards. The brief is the read; the
 * chat is where the partner steers the next night.
 *
 * Data contract (unchanged, load-bearing):
 *  - List reports:  GET /api/storage/apps-list/{appId}/reports/   (cursor-paged)
 *  - Read a brief:  GET /api/storage/apps/{appId}/reports/<date>.html  (TEXT)
 *  - settings.json / state.json: JSON via the same storage base.
 *  - Reports render in a SANDBOXED srcDoc iframe with NO allow-scripts.
 *
 * Brief↔chat link: the cron creates the morning chat (`POST /api/chats`,
 * title "Morning brief — <date>") and SHOULD write a sibling
 * `reports/<date>.meta.json` = { "chat_id": "<id>" } so the date maps to a
 * chat without guessing. The app reads that meta with its app token; when a
 * chat_id resolves it mounts the real ChatView via `window.mobius.chat({
 * mount, chatId })` (the embed runs in the shell origin with the owner JWT —
 * the only path that can read/post an owner-created chat; the app token alone
 * is 403'd on /api/chats). No chat_id (or no `window.mobius.chat`) → the
 * brief stands alone, gracefully.
 */
import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react'

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const VERBOSITY_OPTIONS = [
  { id: 'terse', label: 'Terse', hint: 'A short paragraph — just the highlights.' },
  { id: 'standard', label: 'Standard', hint: 'A few paragraphs, a suggestion or two, one closing thought.' },
  { id: 'chatty', label: 'Chatty', hint: 'A longer narrative with more pattern-spotting.' },
]
const DEFAULT_VERBOSITY = 'standard'

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
  "style-src 'unsafe-inline'",
  'img-src data: blob:',
  'font-src data:',
  "base-uri 'none'",
  "form-action 'none'",
].join('; ')

export function hardenReportHtml(html) {
  const body = typeof html === 'string' ? html : ''
  const meta = `<meta http-equiv="Content-Security-Policy" content="${REPORT_CSP}">`
  if (/<head[\s>]/i.test(body)) return body.replace(/<head([^>]*)>/i, `<head$1>${meta}`)
  if (/<html[\s>]/i.test(body)) return body.replace(/<html([^>]*)>/i, `<html$1><head>${meta}</head>`)
  return `<!doctype html><html><head>${meta}</head><body>${body}</body></html>`
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
// owner JWT and can read/post the owner-created morning chat the cron opened.
// (The app token alone is 403'd on /api/chats; this is the supported path.)
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
          The conversation about this brief isn’t available here. Open it from
          your chat list to reply.
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

// ---------------------------------------------------------------------------
// Report detail — the brief + chat split view.
//
// The brief is the static, script-free HTML the agent authored: rendered in a
// SANDBOXED srcDoc iframe with NO allow-scripts (containment). Beneath it, the
// morning chat. We resolve the chat_id from `reports/<date>.meta.json` (the
// cron's sibling), then mount the embed. Back is wired through
// history.pushState + popstate so the phone's back gesture returns to the
// list (falls back to the in-bar button when history is unavailable).
//
// The brief iframe auto-sizes to its content (the document is short — a
// morning read), measured via the iframe's own scrollHeight after load, so
// the page scrolls as one column (brief, then chat) instead of nesting two
// scroll regions. Same-origin sandbox lets us read contentDocument for that
// measurement; no scripts run inside.
// ---------------------------------------------------------------------------

function ReportDetail({ dateStr, storage, online, onBack }) {
  const [state, setState] = useState({ phase: 'loading', html: '' })
  const [chatId, setChatId] = useState(undefined) // undefined=resolving, null=none, string=id
  const [briefHeight, setBriefHeight] = useState(360)
  const iframeRef = useRef(null)

  // Load the brief body + resolve its chat in parallel.
  useEffect(() => {
    let cancelled = false
    setState({ phase: 'loading', html: '' })
    setChatId(undefined)
    setBriefHeight(360)
    ;(async () => {
      const res = await storage.getReportHtml(`${dateStr}.html`)
      if (cancelled) return
      if (res.data != null) setState({ phase: 'ready', html: hardenReportHtml(res.data) })
      else if (res.notFound) setState({ phase: 'missing', html: '' })
      else setState({ phase: 'error', html: '' })
    })()
    ;(async () => {
      const id = await storage.getReportChatId(dateStr)
      if (!cancelled) setChatId(id)
    })()
    return () => { cancelled = true }
  }, [dateStr, storage])

  // Size the brief iframe to its content so the column scrolls as one. The
  // sandbox is same-origin (no scripts), so contentDocument is readable. We
  // re-measure on a ResizeObserver of the inner body for late layout (web
  // fonts, images) and clamp to a sane range.
  const measure = useCallback(() => {
    const el = iframeRef.current
    if (!el) return
    try {
      const doc = el.contentDocument
      if (!doc || !doc.body) return
      const h = Math.max(doc.body.scrollHeight, doc.documentElement.scrollHeight)
      if (h > 0) setBriefHeight(Math.min(Math.max(h, 200), 100000))
    } catch {
      // Cross-origin guard (shouldn't happen with allow-same-origin) — leave
      // the default height; the inner doc keeps its own scroll as a fallback.
    }
  }, [])

  const onIframeLoad = useCallback(() => {
    measure()
    try {
      const doc = iframeRef.current?.contentDocument
      if (doc && doc.body && typeof ResizeObserver !== 'undefined') {
        const ro = new ResizeObserver(() => measure())
        ro.observe(doc.body)
        // Stash so we can disconnect on unmount via the iframe element.
        iframeRef.current.__ro = ro
      }
    } catch {}
  }, [measure])

  useEffect(() => () => {
    try { iframeRef.current?.__ro?.disconnect() } catch {}
  }, [])

  // Push a history entry on mount so a back gesture pops it; intercept the
  // pop to return to the list instead of leaving the app. If the user backs
  // via the in-app button we go() to consume our own entry so we don't leave
  // a dangling forward state.
  const poppedRef = useRef(false)
  useEffect(() => {
    let pushed = false
    try {
      window.history.pushState({ dreamingDetail: dateStr }, '')
      pushed = true
    } catch {
      pushed = false
    }
    const onPop = () => {
      poppedRef.current = true
      onBack()
    }
    window.addEventListener('popstate', onPop)
    return () => {
      window.removeEventListener('popstate', onPop)
      if (pushed && !poppedRef.current) {
        try { window.history.back() } catch { /* ignore */ }
      }
    }
  }, [dateStr, onBack])

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
              // The report is static HTML/CSS authored by the agent. sandbox
              // WITHOUT allow-scripts is the containment: same-origin so its
              // own <style> + relative anchors resolve (and we can measure its
              // height), but no script execution.
              sandbox="allow-same-origin"
            />
          </div>

          <div style={S.chatPanel}>
            <div style={S.chatHeader}>
              <span style={S.chatHeaderDot} aria-hidden="true" />
              <span style={S.chatHeaderText}>Morning conversation</span>
              {chatId && <span style={S.chatHeaderHint}>tap a card or reply below</span>}
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
                  morning note. When a brief has questions for you, the chat
                  appears here so you can answer with a tap.
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

function SettingsTab({ appId, storage, online }) {
  const [hour, setHour] = useState(DEFAULT_HOUR)
  const [verbosity, setVerbosity] = useState(DEFAULT_VERBOSITY)
  const [excludeApps, setExcludeApps] = useState([])
  const [settingsExtra, setSettingsExtra] = useState({})
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
        if (VERBOSITY_OPTIONS.some((v) => v.id === s.verbosity)) {
          setVerbosity(s.verbosity)
        }
        if (Array.isArray(s.exclude_apps)) setExcludeApps(s.exclude_apps)
      }
      // res.notFound (first run) -> keep the 06:00 / standard defaults.
      setLoading(false)
    })()
    return () => { cancelled = true }
  }, [storage])

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
        verbosity,
        exclude_apps: excludeApps,
        provider: settingsExtra.provider || 'claude',
        model: settingsExtra.model ?? null,
      })
      setToast('Saved ✓')
      setTimeout(() => setToast(''), 2600)
    } catch {
      setError(online ? 'Could not save — try again.' : 'You’re offline — reconnect to save.')
    } finally {
      setSaving(false)
    }
  }, [saving, cronIsCustom, rawCron, hour, verbosity, excludeApps, settingsExtra, storage, online])

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
          <span style={S.sectionIcon} aria-hidden="true">✍️</span>
          <h2 style={S.sectionLabel}>How much detail</h2>
        </div>
        <p style={S.note}>How long and discursive each brief should be.</p>
        <div style={S.verbList} role="radiogroup" aria-label="Verbosity">
          {VERBOSITY_OPTIONS.map((opt) => {
            const on = verbosity === opt.id
            return (
              <div
                key={opt.id}
                style={S.verbRow(on)}
                className="dreaming-pressable"
                onClick={() => setVerbosity(opt.id)}
                role="radio"
                aria-checked={on}
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    setVerbosity(opt.id)
                  }
                }}
              >
                <span style={S.verbRadio(on)} aria-hidden="true" />
                <div style={S.verbMain}>
                  <span style={S.verbTitle}>{opt.label}</span>
                  <span style={S.verbHint}>{opt.hint}</span>
                </div>
              </div>
            )
          })}
        </div>
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

  const closeDetail = useCallback(() => setOpenDate(null), [])

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
            <button style={S.tab(tab === 'settings')} onClick={() => setTab('settings')}>
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
              onOpen={setOpenDate}
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
          <SettingsTab appId={appId} storage={storage} online={online} />
        )}
      </div>
    </div>
  )
}
