/* Reflection — the nightly morning-brief viewer.
 *
 * Lists the dated reports the reflection agent leaves overnight, tracks a
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

// Exit-code meanings for cron_outcome events (from the fetch.sh legend).
const CRON_EXIT_LABELS = {
  0:   'ran ok',
  2:   'config error (no app id)',
  3:   'config error (missing service token)',
  5:   'skipped — a prior run still held the lock',
  124: 'timed out',
  127: 'runner not found',
}

function cronExitLabel(code) {
  const n = Number(code)
  if (n === 0) return 'ran ok'
  if (n === 5) return 'skipped (lock held)'
  if (n === 124) return 'timed out'
  if (n === 2 || n === 3) return 'config error (exit ' + n + ')'
  return 'failed (exit ' + n + ')'
}

// Guarded signal emitter for reflection.
function emitSignal(appId, token, name, data = {}) {
  try {
    const payload = { name, ts: new Date().toISOString(), ...data }
    fetch(`/api/storage/apps/${appId}/signals.jsonl`, {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }).catch(() => {})
  } catch {}
}
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

// Base CSS injected into every brief's <head>, BEFORE the brief's own
// <style>. Two jobs:
//
//  1. Never let a brief overflow the phone horizontally. The owner reported
//     having to scroll sideways: an agent-authored wide table, a long
//     unbroken code line, or an oversized image can blow past the viewport.
//     These rules box everything to 100% width, wrap long text, and turn a
//     wide <table>/<pre> into its OWN horizontal scroller instead of pushing
//     the page sideways. The brief's template sets `box-sizing: border-box`
//     and a centred max-width already; this is the safety net for whatever
//     content the agent writes inside it.
//  2. Style the drill-down + questions affordances the brief now uses so they
//     look native even when the agent hand-writes a minimal fallback brief
//     (no template <style>): <details>/<summary> for "stay high-level by
//     default, detail on tap", and a `.brief-questions` card for the
//     end-of-brief "A few questions for you" block. The template's own richer
//     styles win where present (these are element/low-specificity defaults
//     that the cascade lets a later .item/.decision rule override).
//
// Theme tokens (--accent, --surface, --border, --muted) come from the brief's
// own :root; we fall back to sensible literals so a token-less fallback brief
// still reads well.
const REPORT_BASE_STYLE = `<style>
  html, body { max-width: 100%; overflow-x: hidden; margin: 0; }
  * { box-sizing: border-box; }
  *:not(html):not(body) { max-width: 100%; }
  img, svg, video, canvas { max-width: 100%; height: auto; }
  pre, code {
    white-space: pre-wrap;
    word-break: break-word;
    overflow-wrap: anywhere;
  }
  pre { overflow-x: auto; }
  table { display: block; overflow-x: auto; max-width: 100%; }

  /* Drill-down: terse by default, detail on tap. */
  details {
    margin: 12px 0;
    border: 1px solid var(--border, #e4e1dc);
    border-radius: 12px;
    background: var(--surface, #fff);
    overflow: hidden;
  }
  details > summary {
    cursor: pointer;
    padding: 12px 14px;
    font-weight: 600;
    list-style: none;
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--text, #1c1b19);
  }
  details > summary::-webkit-details-marker { display: none; }
  details > summary::before {
    content: "›";
    display: inline-block;
    transition: transform .15s ease;
    color: var(--accent, #6d5ef0);
    font-weight: 700;
  }
  details[open] > summary::before { transform: rotate(90deg); }
  details > summary:focus-visible {
    outline: 2px solid var(--accent, #6d5ef0);
    outline-offset: 2px;
  }
  details > *:not(summary) {
    padding: 0 14px 12px;
    margin-top: 0;
  }

  /* End-of-brief questions card — "A few questions for you". */
  .brief-questions {
    margin: 28px 0 8px;
    padding: 16px 18px;
    border: 1px solid var(--accent, #6d5ef0);
    border-left-width: 3px;
    border-radius: 14px;
    background: var(--accent-tint, #ece9fd);
  }
  .brief-questions > h2,
  .brief-questions > h3 {
    margin: 0 0 10px;
    font-size: 1.1rem;
    color: var(--text, #1c1b19);
  }
  .brief-questions ol,
  .brief-questions ul {
    margin: 0;
    padding-left: 1.2em;
  }
  .brief-questions li { margin: 8px 0; line-height: 1.5; }
  .brief-questions .q-note {
    margin-top: 12px;
    font-size: 0.9rem;
    color: var(--muted, #6b6862);
  }
</style>`

// Injected into every brief's <head>. Reports the content height to the
// parent via postMessage so the parent can size the iframe without needing
// allow-same-origin (which would give the iframe the shell origin and its
// owner JWT). The script is intentionally tiny — no external deps, no
// network calls. The CSP above allows 'unsafe-inline' scripts precisely
// for this snippet; together with the absence of allow-same-origin the
// iframe's origin is null and it cannot reach the parent's DOM or storage.
//
// Measurement: documentElement.getBoundingClientRect().height is the html
// element's border-box height, which tracks content (REPORT_BASE_STYLE sets
// html/body margin to 0). Unlike scrollHeight it is NOT floored at the
// iframe's own viewport height, so a transient over-measurement taken
// mid-reflow (classic scrollbars appearing shrink the layout width and
// re-wrap text taller for a frame) shrinks back on the next emit instead
// of ratcheting the iframe height up forever.
const REPORT_HEIGHT_SCRIPT = `<script>
(function(){
  function emit(){
    var h=Math.ceil(document.documentElement.getBoundingClientRect().height);
    if(h>0)parent.postMessage({type:'reflection:brief-height',height:h},'*');
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
  // Order: CSP first, then the base style (overflow guards + details/questions
  // defaults), then the height reporter. The base style sits before the
  // brief's own <style> so the template's richer rules win on the cascade,
  // while the html/body overflow guards (which the template never sets) hold.
  const inject = `<meta http-equiv="Content-Security-Policy" content="${REPORT_CSP}">${REPORT_BASE_STYLE}${REPORT_HEIGHT_SCRIPT}`
  if (/<head[\s>]/i.test(body)) return body.replace(/<head([^>]*)>/i, `<head$1>${inject}`)
  if (/<html[\s>]/i.test(body)) return body.replace(/<html([^>]*)>/i, `<html$1><head>${inject}</head>`)
  return `<!doctype html><html><head>${inject}</head><body>${body}</body></html>`
}

// Validate + coerce the in-report question carrier's questions array into the
// exact shape the native card consumes: [{ question, header, multiSelect,
// options:[{label, description}] }]. Anything malformed is dropped, not
// repaired — a half-formed question is worse than a missing one. Caps at 3
// questions and 6 options each so a runaway carrier can't flood the read.
// Mirrors the News app's copy (app-news/index.jsx) so the two stay in step.
export function sanitizeQuestions(arr) {
  if (!Array.isArray(arr)) return []
  const out = []
  for (const raw of arr) {
    if (out.length >= 3) break        // cap at 3 VALID questions, not 3 inputs
    if (!raw || typeof raw !== 'object') continue
    const question = typeof raw.question === 'string' ? raw.question.trim() : ''
    if (!question) continue
    // Dedupe by exact question text so answers can be keyed by text downstream
    // without collisions (the persisted answers object is keyed by question).
    if (out.some((q) => q.question === question)) continue
    const opts = Array.isArray(raw.options) ? raw.options : []
    const options = []
    for (const o of opts.slice(0, 6)) {
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

// Pull the agent's declarative in-report questions out of the RAW brief HTML,
// returning the HTML with the carrier removed so it never reaches the sandboxed
// iframe. The brief agent emits ONE inert JSON carrier as a sibling after the
// brief's root element:
//
//   <section class="report-questions" data-report-questions>
//     <h2>…</h2><p class="rq-note">…</p>
//     <script type="application/mobius-questions+json">{ … }</script>
//   </section>
//
// The <script> is inert inside the sandboxed iframe (null origin -> never
// executes), but if it reached srcDoc the visible <section>/<h2> shell would
// render as an empty "questions" heading in the brief. So we extract the JSON
// here and STRIP the carrier before hardenReportHtml, then render native tap
// cards below the brief (see ReportQuestions).
//
// Regex-based on purpose (no DOMParser) so it's identical to the News app's
// copy and safe to run before hardenReportHtml. The matcher is deliberately
// narrow — one carrier, the platform-specific MIME type — so it can't
// swallow an ordinary <section> the brief happens to use. Returns { html,
// questions }: html with the carrier stripped; questions = a validated array
// (the EXACT shell QuestionCard shape) or [] when absent/malformed. Never
// throws — the brief is the floor, a bad carrier just means no cards.
export function extractReportQuestions(html) {
  const empty = { html: typeof html === 'string' ? html : '', questions: [] }
  if (typeof html !== 'string') return empty
  // Whitespace-tolerant type attribute (type = "…") so a stray space can't
  // smuggle the carrier past the matcher into srcDoc.
  const scriptRe = /<script\b[^>]*type\s*=\s*["']application\/mobius-questions\+json["'][^>]*>([\s\S]*?)<\/script>/i
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
  // Strip ORDER matters. Remove ALL carrier scripts FIRST (global) — that
  // deletes any literal </section> hiding inside the JSON, so the section's
  // remaining inner text can no longer terminate the non-greedy wrapper match
  // early. THEN remove ALL now-script-free wrappers (global) so the visible
  // shell never reaches the sandboxed iframe. Both passes are global so a
  // second carrier can't survive.
  let out = html
  out = out.replace(/<script\b[^>]*type\s*=\s*["']application\/mobius-questions\+json["'][^>]*>[\s\S]*?<\/script>/gi, '')
  out = out.replace(/<(section|div)\b[^>]*\bdata-report-questions\b[^>]*>[\s\S]*?<\/\1>/gi, '')
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
// work; the violet accent is the one committed hardcode. A handful of
// keyframes (drift, shimmer, rise) feed loading + entrance states so they feel
// alive rather than static.
//
// One module-level `const CSS` is rendered once at the app root as
// <style>{CSS}</style>; the app's class prefix is `dr-`. The shared chrome
// (Root / Header / Sheet / Empty / Card / Button / Input / Segmented /
// ChatEmbed / SyncPill) is fenced with `/* mobius-ui:<Block> v1 … */` markers
// so a future extraction is mechanical; app-specific blocks (aurora, moon
// tile, date tile, streak badge, brief↔chat split, settings) stay below as
// unfenced `dr-` rules and keep their exact current values. The violet
// accent (#7c6cf0 / #a78bfa) is the one committed hardcode the theme can't
// express. Structural colors are theme tokens.
// ---------------------------------------------------------------------------

const ACCENT = '#7c6cf0'        // reflection's own violet
const ACCENT_2 = '#a78bfa'      // lighter companion for gradients/glows
const ACCENT_DIM = 'rgba(124,108,240,0.13)'
const ACCENT_DIM_2 = 'rgba(167,139,250,0.10)'

const CSS = `
@keyframes dr-drift {
  0%   { transform: translateY(0) rotate(0deg); opacity: .85; }
  50%  { transform: translateY(-6px) rotate(4deg); opacity: 1; }
  100% { transform: translateY(0) rotate(0deg); opacity: .85; }
}
@keyframes dr-rise {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes dr-spin {
  to { transform: rotate(360deg); }
}

/* mobius-ui:Focus v1 -- shared keyboard focus ring (WCAG 2.4.7); never bare outline:none */
:where(button,a,input,textarea,select,summary,[role="button"],[tabindex]:not([tabindex="-1"])):focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
/* /mobius-ui:Focus */

/* mobius-ui:Root v1 — keep in sync; library candidate. Diverge below the marker only. */
.dr-root {
  position: relative;
  display: flex; flex-direction: column;
  height: 100%; width: 100%; max-width: 100%;
  overflow-x: hidden;
  padding-left: env(safe-area-inset-left);
  padding-right: env(safe-area-inset-right);
  background: var(--bg); color: var(--text); font-family: var(--font);
  -webkit-font-smoothing: antialiased;
  -webkit-tap-highlight-color: transparent;
}
.dr-scroll {
  flex: 1; min-height: 0;
  overflow-y: auto; overflow-x: hidden;
  padding: 16px 20px 40px;
  word-break: break-word; overflow-wrap: anywhere;
  position: relative; z-index: 1;
  overscroll-behavior: contain;
}
/* /mobius-ui:Root */

/* mobius-ui:Header v1 — keep in sync; library candidate. Diverge below the marker only. */
.dr-header {
  flex: 0 0 auto;
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  flex-wrap: wrap;
  padding: max(22px, env(safe-area-inset-top)) 20px 0;
  position: relative; z-index: 1;
}
.dr-brand { display: flex; align-items: center; gap: 11px; min-width: 0; }
.dr-brand-icon {
  flex: 0 0 auto; width: 26px; height: 26px; border-radius: 6px;
  object-fit: cover; display: block;
}
.dr-brand-fallback {
  flex: 0 0 auto; width: 26px; height: 26px; border-radius: 6px;
  align-items: center; justify-content: center;
  background: ${ACCENT}; color: var(--bg, #0c0c0c);
  font-weight: 700; line-height: 1;
}
.dr-header-right { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; flex: 0 0 auto; position: relative; z-index: 1; }
/* /mobius-ui:Header */

/* mobius-ui:Empty v1 — keep in sync; library candidate. Diverge below the marker only. */
.dr-empty {
  display: flex; flex-direction: column; align-items: center; text-align: center;
  max-width: 440px; margin: 0 auto; padding: 60px 24px 40px;
  color: var(--muted); font-size: 14px; line-height: 1.65;
}
.dr-empty-mark {
  width: 74px; height: 74px; margin: 0 auto 18px; border-radius: 22px;
  display: flex; align-items: center; justify-content: center;
  background: linear-gradient(160deg, ${ACCENT_DIM} 0%, ${ACCENT_DIM_2} 100%);
  border: 1px solid var(--border);
}
.dr-empty-mark-glyph { font-size: 34px; animation: dr-drift 6s ease-in-out infinite; }
.dr-empty-title { font-size: 17px; font-weight: 700; color: var(--text); letter-spacing: -0.2px; margin-bottom: 8px; }
/* /mobius-ui:Empty */

/* mobius-ui:Card v1 — keep in sync; library candidate. Diverge below the marker only. */
.dr-card {
  display: flex; align-items: stretch; gap: 14px; width: 100%; min-height: 44px;
  padding: 15px 16px; text-align: left;
  background: var(--surface); color: var(--text); font-family: var(--font);
  border: 1px solid var(--border); border-radius: 16px;
  position: relative; overflow: hidden; cursor: pointer;
  transition: border-color .16s ease, transform .12s ease, box-shadow .16s ease, background .16s ease;
  touch-action: manipulation; user-select: none;
}
button.dr-card { cursor: pointer; }
@media (hover:hover) { .dr-card:hover { border-color: ${ACCENT}; box-shadow: 0 6px 22px -12px ${ACCENT}; } }
.dr-card:active { transform: scale(.992); background: var(--surface-active, var(--surface)); }
.dr-card:focus-visible { outline: 2px solid ${ACCENT}; outline-offset: 2px; }
.dr-card.is-latest { border-left: 3px solid ${ACCENT}; }
/* /mobius-ui:Card */

/* mobius-ui:Button v1 — keep in sync; library candidate. Diverge below the marker only. */
.dr-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 5px;
  min-height: 44px; padding: 10px 16px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--surface); color: var(--text);
  font-family: var(--font); font-size: 13px; font-weight: 650; cursor: pointer; white-space: nowrap;
  transition: background .14s ease, border-color .14s ease, transform .1s ease, color .14s ease;
  touch-action: manipulation; user-select: none;
}
.dr-btn:active { transform: scale(.97); opacity: 0.85; }
.dr-btn:focus-visible { outline: 2px solid ${ACCENT}; outline-offset: 2px; }
.dr-btn:disabled { opacity: 0.5; cursor: default; transform: none; }
/* /mobius-ui:Button */

/* mobius-ui:Segmented v1 — keep in sync; library candidate. Diverge below the marker only. */
.dr-seg {
  display: flex; gap: 2px; padding: 3px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
}
.dr-seg-btn {
  min-height: 44px; padding: 6px 15px; border: none; border-radius: 7px;
  background: transparent; color: var(--muted); font-family: var(--font);
  font-size: 13px; font-weight: 650; cursor: pointer; transition: background .15s, color .15s;
  touch-action: manipulation; user-select: none;
}
@media (hover:hover) { .dr-seg-btn:hover { color: var(--text); } }
.dr-seg-btn.is-active { background: ${ACCENT}; color: #fff; }
/* /mobius-ui:Segmented */

/* mobius-ui:ChatEmbed v1 — keep in sync; library candidate. Diverge below the marker only. */
.dr-chat-embed {
  flex: 1 1 auto;
  min-height: 0;   /* the flexbox overflow fix — lets the iframe scroll internally */
  width: 100%;
  overflow: hidden;
  background: var(--bg);
}
.dr-chat-embed iframe { display: block; width: 100%; height: 100%; border: 0; }
/* /mobius-ui:ChatEmbed */

/* mobius-ui:Spinner v1 — keep in sync; library candidate. */
.dr-spinner {
  width: 26px; height: 26px; border-radius: 50%;
  border: 2.5px solid ${ACCENT_DIM}; border-top-color: ${ACCENT};
  animation: dr-spin 0.8s linear infinite;
}
.dr-spinner-sm { width: 16px; height: 16px; border-width: 2px; }
@media (prefers-reduced-motion: reduce) { .dr-spinner { animation: none; } }
/* /mobius-ui:Spinner */

/* mobius-ui:Scrollskin v1 — keep in sync; library candidate. Add the \`dr-scroll\` class to a scroller. */
.dr-scroll::-webkit-scrollbar { width: 9px; height: 9px; }
.dr-scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 99px; border: 2px solid transparent; background-clip: padding-box; }
.dr-scroll::-webkit-scrollbar-thumb:hover { background: var(--muted); background-clip: padding-box; }
/* /mobius-ui:Scrollskin */

/* ---- App-specific (reflection) — keep exact current values ---- */

/* The detail view reuses the shared empty shape for its missing/error notes,
   but with a tighter top inset than the list's first-run empty. */
.dr-empty.is-compact { padding-top: 56px; }

/* Entrance + press affordances. A handful of reflection surfaces ride a small
   rise-in on mount; pressable controls get the same active/focus feel as the
   shared card without being one. */
.dr-rise { animation: dr-rise .32s cubic-bezier(.22,.61,.36,1) both; }
.dr-pressable { transition: background .14s ease, border-color .14s ease, transform .1s ease, color .14s ease; touch-action: manipulation; user-select: none; }
.dr-pressable:active { transform: scale(.97); opacity: 0.85; }
.dr-pressable:focus-visible { outline: 2px solid ${ACCENT}; outline-offset: 2px; }

/* A faint aurora wash behind the header — pure decoration, pointer-none, so
   the top of the app reads as a sky rather than a flat bar. */
.dr-aurora {
  position: absolute; top: 0; left: 0; right: 0; height: 220px;
  background: radial-gradient(120% 90% at 18% -10%, ${ACCENT_DIM} 0%, transparent 55%), radial-gradient(110% 80% at 92% -20%, ${ACCENT_DIM_2} 0%, transparent 60%);
  pointer-events: none; z-index: 0;
}
.dr-divider { height: 1px; background: var(--border); margin: 16px 20px 0; position: relative; z-index: 1; }

/* Streak badge — header pill + standalone streak bar share the base; the bar
   variant bumps padding + font. */
.dr-streak-badge {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 5px 11px; border-radius: 999px;
  background: ${ACCENT_DIM}; color: ${ACCENT};
  border: 1px solid transparent;
  font-size: 12.5px; font-weight: 650; line-height: 1.2; white-space: nowrap;
}
.dr-streak-badge.is-quiet {
  background: var(--surface); color: var(--muted); border-color: var(--border);
}
.dr-streak-bar { max-width: 660px; margin: 0 auto 16px; display: flex; }
.dr-streak-bar .dr-streak-badge { padding: 7px 13px; font-size: 13px; }
.dr-streak-flame { animation: dr-drift 4s ease-in-out infinite; }
.dr-streak-num { font-weight: 750; }
.dr-streak-unit { font-weight: 550; }
.dr-streak-dots { margin-left: 4px; letter-spacing: 1px; opacity: 0.55; font-size: 9px; }

/* Reports list + dated card */
.dr-list { display: flex; flex-direction: column; gap: 11px; max-width: 660px; margin: 0 auto; }
.dr-date-tile {
  width: 46px; flex-shrink: 0; border-radius: 12px;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 0; align-self: center;
  background: ${ACCENT_DIM}; color: ${ACCENT};
  padding: 8px 0; line-height: 1;
}
.dr-date-tile.is-latest {
  background: linear-gradient(160deg, ${ACCENT} 0%, ${ACCENT_2} 100%); color: #fff;
}
.dr-date-tile-day { font-size: 10px; font-weight: 700; letter-spacing: 0.5px; text-transform: uppercase; opacity: 0.92; }
.dr-date-tile-num { font-size: 19px; font-weight: 750; letter-spacing: -0.5px; }
.dr-card-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; justify-content: center; }
.dr-card-label-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.dr-card-label { font-size: 16px; font-weight: 700; letter-spacing: -0.2px; line-height: 1.2; }
.dr-card-sub { font-size: 12px; color: var(--muted); font-weight: 500; }
.dr-card-tldr {
  font-size: 13px; color: var(--muted); line-height: 1.5; margin-top: 5px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.dr-card-chevron { align-self: center; font-size: 20px; color: var(--muted); flex-shrink: 0; line-height: 1; opacity: 0.7; }
.dr-latest-pill {
  font-size: 11px; font-weight: 750; letter-spacing: 0.7px;
  text-transform: uppercase; color: #fff;
  background: ${ACCENT}; padding: 2px 8px; border-radius: 999px;
}

/* Loading / error / offline states */
.dr-loading-wrap { text-align: center; padding: 64px 24px; color: var(--muted); font-size: 13px; }
.dr-loading-wrap .dr-spinner { margin: 0 auto 14px; }
.dr-error-box {
  max-width: 660px; margin: 0 auto; padding: 16px; border-radius: 14px;
  border: 1px solid var(--border); background: var(--surface);
  color: var(--text); font-size: 13px; line-height: 1.55;
  display: flex; flex-direction: column; gap: 10px;
}
.dr-retry-btn {
  align-self: flex-start; padding: 7px 14px; border-radius: 9px;
  border: 1px solid ${ACCENT}; background: transparent; color: ${ACCENT};
  font-size: 12.5px; font-weight: 650; cursor: pointer; font-family: var(--font);
  touch-action: manipulation; user-select: none;
}
.dr-offline-banner {
  max-width: 660px; margin: 0 auto 14px; padding: 10px 14px;
  border-radius: 12px; background: ${ACCENT_DIM}; border: 1px solid var(--border);
  color: var(--text); font-size: 12.5px; line-height: 1.45;
  display: flex; align-items: center; gap: 8px;
}

/* Report detail — brief + chat split view */
.dr-detail {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  background: var(--bg); z-index: 5;
}
.dr-detail-bar {
  display: flex; align-items: center; gap: 12px;
  padding: max(11px, env(safe-area-inset-top)) 14px 11px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0; background: var(--surface);
}
.dr-back-btn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 7px 13px 7px 9px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--bg);
  color: var(--text); font-size: 13px; font-weight: 650;
  cursor: pointer; font-family: var(--font); flex-shrink: 0;
  touch-action: manipulation; user-select: none;
}
.dr-back-glyph { font-size: 16px; }
.dr-detail-title { display: flex; flex-direction: column; min-width: 0; line-height: 1.25; flex: 1; }
.dr-detail-title-main { font-size: 15px; font-weight: 700; letter-spacing: -0.2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.dr-detail-title-sub { font-size: 12px; color: var(--muted); font-weight: 500; }
.dr-split-body {
  flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden;
  display: flex; flex-direction: column;
  overscroll-behavior: contain;
  /* Reserve the classic-scrollbar gutter even while content fits, so the
     height-bridge growing the brief iframe never changes the content width
     (width change → text re-wrap → new height → feedback loop). */
  scrollbar-gutter: stable;
}
.dr-brief-panel {
  flex-shrink: 0; display: flex; flex-direction: column;
  border-bottom: 1px solid var(--border);
}
.dr-brief-iframe { width: 100%; border: none; background: var(--bg); display: block; }
.dr-brief-loading {
  min-height: 320px; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 12px;
  color: var(--muted); font-size: 13px;
}
.dr-chat-mount-wrap {
  position: relative;
  flex: 1 1 auto; min-height: 0;
  display: flex; flex-direction: column;
}
.dr-chat-panel { flex-shrink: 0; display: flex; flex-direction: column; background: var(--bg); }
/* With a live conversation the panel takes a real share of the viewport
   (the house bottom-chat-pane model; cf. app-latex's 50/50 split). The chain
   must keep every height DEFINITE: panel 60dvh → mount-wrap flex:1 → embed
   flex:1 → iframe height:100%. If no ancestor has a definite height, the
   iframe's height:100% resolves to auto and the chat collapses to the ~150px
   iframe intrinsic default, leaving a dead band of empty wrapper below the
   composer. Viewport units, not a percentage: a % height here resolves
   against the scroll column and Chrome treats it as auto (content-sized),
   silently re-creating the collapsed chat. While resolving / chatless the
   panel stays content-sized so the spinner and the no-chat note don't
   reserve blank space. */
.dr-chat-panel.is-live { height: 60vh; height: 60dvh; min-height: 340px; }
.dr-chat-header { display: flex; align-items: center; gap: 8px; padding: 13px 16px 9px; flex-shrink: 0; }
.dr-chat-header-dot {
  width: 7px; height: 7px; border-radius: 50%; background: ${ACCENT};
  box-shadow: 0 0 0 4px ${ACCENT_DIM}; flex-shrink: 0;
}
.dr-chat-header-text { font-size: 13px; font-weight: 700; letter-spacing: -0.1px; }
.dr-chat-header-hint { font-size: 12px; color: var(--muted); font-weight: 500; margin-left: auto; }
.dr-chat-resolving {
  padding: 20px 16px 28px; display: flex; align-items: center; gap: 10px;
  color: var(--muted); font-size: 12.5px;
}
.dr-no-chat-note {
  margin: 14px 16px 22px; padding: 14px 16px; border-radius: 13px;
  background: var(--surface); border: 1px dashed var(--border);
  color: var(--muted); font-size: 12.5px; line-height: 1.55;
  display: flex; align-items: flex-start; gap: 10px;
}
.dr-no-chat-glyph { font-size: 15px; line-height: 1.2; }
/* Sits flush under the embedded chat's composer — the border is the only
   separation, so there is no dead band between the chat and the button. */
.dr-feedback-row {
  flex-shrink: 0; margin: 0 16px;
  padding: 12px 0 max(14px, env(safe-area-inset-bottom));
  border-top: 1px solid var(--border);
}
.dr-feedback-btn {
  display: flex; align-items: center; justify-content: center; gap: 7px;
  width: 100%; min-height: 46px; padding: 11px 16px; border-radius: 12px;
  border: 1px solid ${ACCENT}; background: ${ACCENT_DIM};
  color: ${ACCENT}; font-size: 13.5px; font-weight: 700;
  cursor: pointer; font-family: var(--font);
  touch-action: manipulation; user-select: none;
}

/* Last-night status row */
.dr-status-row {
  max-width: 660px; margin: 0 auto 14px; display: flex; align-items: center;
  gap: 10px; padding: 10px 14px; border-radius: 13px;
  border: 1px solid var(--border); background: var(--surface);
  font-size: 12.5px; line-height: 1.45; flex-wrap: wrap;
}
.dr-status-dot {
  width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
}
.dr-status-dot.ok   { background: var(--green, #3fb950); box-shadow: 0 0 0 3px rgba(63,185,80,.15); }
.dr-status-dot.fail { background: var(--danger, #f85149); box-shadow: 0 0 0 3px rgba(248,81,73,.15); }
.dr-status-dot.skip { background: var(--muted); }
.dr-status-dot.none { background: var(--border); }
.dr-status-label { flex: 1; color: var(--text); font-weight: 600; }
.dr-status-hint  { color: var(--muted); font-size: 12px; }
.dr-status-investigate {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 5px 12px; border-radius: 9px; border: 1px solid var(--danger, #f85149);
  background: transparent; color: var(--danger, #f85149);
  font-size: 12px; font-weight: 650; cursor: pointer; font-family: var(--font);
  touch-action: manipulation; user-select: none;
}

/* Settings */
.dr-settings-wrap { max-width: 580px; margin: 0 auto; display: flex; flex-direction: column; gap: 22px; }
.dr-settings-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px; padding: 18px; display: flex; flex-direction: column; gap: 10px;
}
.dr-section-head { display: flex; align-items: center; gap: 10px; }
.dr-section-icon {
  width: 30px; height: 30px; border-radius: 9px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  background: ${ACCENT_DIM}; font-size: 15px;
}
.dr-section-label { font-size: 14.5px; font-weight: 700; letter-spacing: -0.1px; margin: 0; }
.dr-note { font-size: 12.5px; color: var(--muted); margin: 0; line-height: 1.55; }
.dr-note-strong { color: var(--text); font-weight: 650; }
.dr-time-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 2px; }
.dr-time-input {
  padding: 9px 12px; font-size: 16px; font-family: var(--font); font-weight: 600;
  background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: 10px;
  width: 132px;
}
.dr-time-input:focus:not(:focus-visible) { outline: none; }
.dr-custom-cron-note {
  font-size: 12px; color: var(--muted); line-height: 1.5;
  padding: 10px 12px; border-radius: 11px;
  background: var(--bg); border: 1px solid var(--border); margin-top: 2px;
}
.dr-custom-cron-note .dr-time-row { margin-top: 10px; }
.dr-select {
  width: 100%; min-height: 44px; padding: 9px 12px;
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg); color: var(--text); font-size: 16px;
  font-family: var(--font); font-weight: 650;
  touch-action: manipulation; user-select: none;
}
.dr-select:focus:not(:focus-visible) { outline: none; }
.dr-meta {
  font-size: 12px; color: var(--muted); line-height: 1.5;
  font-family: var(--mono, var(--font));
  padding: 10px 12px; border-radius: 11px;
  background: var(--bg); border: 1px solid var(--border);
}
.dr-model-label {
  font-size: 12px; color: var(--muted); font-weight: 750;
  text-transform: uppercase; letter-spacing: 0.4px; margin-top: 4px;
}
.dr-textarea {
  width: 100%; min-height: 64px; padding: 9px 12px;
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg); color: var(--text); font-size: 14px;
  font-family: var(--font); resize: vertical; line-height: 1.5;
  box-sizing: border-box;
}
.dr-textarea:focus:not(:focus-visible) { outline: none; }
.dr-verbosity-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
.dr-verb-btn {
  flex: 1; min-height: 44px; padding: 8px 12px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--bg); color: var(--muted);
  font-size: 13px; font-weight: 650; cursor: pointer; font-family: var(--font);
  transition: background .14s, border-color .14s, color .14s;
  touch-action: manipulation; user-select: none; text-align: center;
}
.dr-verb-btn.is-active { border-color: ${ACCENT}; color: ${ACCENT}; background: ${ACCENT_DIM}; }
.dr-verb-hint { font-size: 11.5px; color: var(--muted); margin-top: 4px; line-height: 1.45; }
.dr-save-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 2px; }
.dr-save-btn {
  padding: 10px 22px; border-radius: 12px; border: none;
  background: ${ACCENT}; color: #fff;
  font-size: 13.5px; font-weight: 700; cursor: pointer;
  font-family: var(--font); transition: background 0.15s, opacity .15s;
  box-shadow: 0 6px 18px -8px ${ACCENT};
  touch-action: manipulation; user-select: none;
}
.dr-save-btn:disabled {
  background: var(--surface); color: var(--muted); cursor: default; box-shadow: none;
}
.dr-toast { font-size: 12.5px; color: var(--green, #3fb950); font-weight: 650; }
.dr-error-toast { font-size: 12.5px; color: var(--danger, #f85149); font-weight: 650; }
/* Dead-letter banner: a previously-"Saved" offline write the server later
   refused on drain. Lives at the app root (the originating form is usually
   unmounted by drain time) and reads as an honest retraction, not a toast. */
.dr-deadletter {
  display: flex; align-items: flex-start; gap: 10px;
  margin: 14px 20px 0; padding: 11px 13px;
  border-radius: 12px;
  font-size: 12.5px; line-height: 1.5; color: var(--danger, #f85149);
  background: var(--surface); border: 1px solid var(--danger, #f85149);
}
.dr-deadletter__x {
  flex: none; margin-left: auto; border: none; background: none;
  color: var(--muted); font-size: 16px; line-height: 1;
  padding: 0 2px; cursor: pointer;
  touch-action: manipulation; user-select: none;
}
.dr-schedule-hint {
  font-size: 12px; color: var(--muted); line-height: 1.55;
  padding: 11px 13px; border-radius: 12px;
  background: ${ACCENT_DIM}; border: 1px solid var(--border);
  display: flex; align-items: flex-start; gap: 8px;
}

/* In-brief question cards. The agent embeds these declaratively in the brief
   HTML (a JSON carrier inside an inert <script>); the app renders them natively
   here so the partner taps an answer that's saved for the NEXT run — never a
   live agent the way a background AskUserQuestion would park a server-orphaned
   future. Shape mirrors the shell's QuestionCard; styling mirrors the News
   app's nw-rq, recoloured to Reflection's violet accent. */
.dr-rq {
  margin: 18px 16px 22px;
  padding: 16px 16px 18px;
  border-radius: 14px;
  border: 1px solid ${ACCENT};
  background: ${ACCENT_DIM};
}
.dr-rq__title { font-size: 15px; font-weight: 750; color: var(--text); margin: 0 0 4px; letter-spacing: -0.1px; }
.dr-rq__note { font-size: 12px; color: var(--muted); margin: 0 0 14px; line-height: 1.5; }
.dr-rq__q + .dr-rq__q {
  margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border);
}
.dr-rq__header {
  font-size: 11px; font-weight: 600; text-transform: uppercase;
  letter-spacing: .5px; color: ${ACCENT}; margin-bottom: 4px;
}
.dr-rq__text { font-size: 14px; margin-bottom: 6px; color: var(--text); line-height: 1.45; }
.dr-rq__hint { font-size: 11px; color: var(--muted); margin-bottom: 8px; }
.dr-rq__opts { display: flex; flex-wrap: wrap; gap: 6px; }
.dr-rq__opt {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 8px 13px; min-height: 38px;
  border-radius: 9px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text);
  font-size: 13px; cursor: pointer; box-sizing: border-box;
  font-family: var(--font); touch-action: manipulation; user-select: none;
}
@media (hover: hover) {
  .dr-rq__opt:not(.dr-rq__opt--on):hover { border-color: ${ACCENT}; }
}
.dr-rq__opt--on { background: ${ACCENT}; color: #fff; border-color: ${ACCENT}; }
.dr-rq__opt--dim { opacity: 0.4; border-color: transparent; }
.dr-rq__opt:disabled { cursor: default; }
.dr-rq__submit {
  display: block; width: 100%; margin-top: 14px; min-height: 44px;
  padding: 11px; border-radius: 11px; border: none;
  background: ${ACCENT}; color: #fff;
  font-size: 14px; font-weight: 700; cursor: pointer;
  font-family: var(--font); touch-action: manipulation; user-select: none;
  box-shadow: 0 6px 18px -8px ${ACCENT};
}
.dr-rq__submit:disabled { opacity: 0.4; cursor: default; box-shadow: none; }
.dr-rq--answered .dr-rq__done {
  margin-top: 14px; font-size: 12.5px; color: var(--muted); line-height: 1.5;
}
.dr-rq__error {
  margin-top: 10px; font-size: 12.5px; color: var(--danger, #f85149); line-height: 1.5;
}

@media (prefers-reduced-motion: reduce) {
  .dr-rise, .dr-empty-mark-glyph, .dr-streak-flame { animation: none !important; }
}

/* mobius-ui:ReducedMotion v1 -- honor the OS reduce-motion setting */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
/* /mobius-ui:ReducedMotion */
`


// ---------------------------------------------------------------------------
// Storage — raw fetch with the app token (per the data contract). JSON paths
// parse JSON; report bodies are read as text. A real 404 (storage empty on
// first run) is normal and returns the `notFound` shape so callers can tell
// it apart from a network failure (`error`) and treat each correctly.
// ---------------------------------------------------------------------------

export function makeStorage(appId, token) {
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

  // The honest save. durableWrite RESOLVES { durability:'synced'|'queued' } —
  // BOTH are durable: 'synced' is server-accepted, 'queued' is outboxed offline
  // with a guaranteed retry (NOT a failure). It REJECTS a DurableWriteError only
  // when the server FATALLY refuses the write (413 quota / 400 / 403): that
  // rejection is the truth the old re-read dance had to reconstruct, so we let it
  // propagate to the caller, which turns it into an error instead of "Saved".
  async function putJSON(path, obj) {
    const dw = (typeof window !== 'undefined' && window.mobius && window.mobius.durableWrite) || null
    if (dw) {
      // Resolve (synced OR queued) = durable success; a fatal reject throws
      // DurableWriteError, which the call site catches and surfaces as an error.
      return await dw(path, obj)
    }
    // Standalone fallback (no window.mobius bridge): a raw PUT, throwing on any
    // non-2xx so the caller treats it exactly like a fatal durableWrite reject.
    // Return the same { durability } shape so callers never special-case the path.
    const r = await fetch(`${base}/${path}`, {
      method: 'PUT',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify(obj),
    })
    if (!r.ok) throw new Error(`PUT ${path} failed (${r.status})`)
    return { durability: 'synced', path }
  }

  async function getReportHtml(name) {
    // Report bodies are raw HTML documents — read as text, not JSON.
    // `cache: 'no-store'` is load-bearing: the nightly cron can RE-AUTHOR a
    // brief for the same date (a corrected or expanded morning note), so a
    // browser/SW cached copy keyed on the unchanged URL would serve the stale
    // body. Force a fresh read each open so a re-authored brief shows.
    try {
      const r = await fetch(`${base}/reports/${name}`, { headers, cache: 'no-store' })
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

  // Subscribe to a JSON path so the list refreshes live when the cron writes a
  // new brief. The nightly run updates state.json (last_run + streak) on every
  // pass, so a change there is the reliable "a new brief just landed" signal —
  // the reports listing has no subscribe of its own. Returns an unsubscribe
  // fn, or a no-op when the runtime bridge is absent (standalone).
  function subscribeJSON(path, cb) {
    if (ms && typeof ms.subscribe === 'function') {
      try { return ms.subscribe(path, cb) } catch { return () => {} }
    }
    return () => {}
  }

  return { getJSON, putJSON, getReportHtml, getReportChatId, listReportDates, subscribeJSON }
}

// ---------------------------------------------------------------------------
// Tiny offline snapshot. Reflection reads via direct fetch (text bodies +
// apps-list), neither of which the platform read-cache covers, so we keep our
// own localStorage snapshot: the dates list, the streak, and the latest
// summary. This is read-only mirror state — only the cron writes reports, so
// the snapshot just paints the same thing the user saw before they lost
// connectivity. Bodies aren't cached here (the report iframe re-fetches; if
// offline it shows a graceful error), only the cheap list metadata.
// ---------------------------------------------------------------------------

const CACHE_VERSION = 1
function cacheKey(appId) { return `reflection:${appId}:list:v${CACHE_VERSION}` }

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

function MorningChat({ chatId, onPhase }) {
  const mountRef = useRef(null)
  const [phase, setPhaseState] = useState('mounting') // mounting | live | unavailable
  // The parent sizes the chat panel from the phase — a live chat gets a real
  // viewport share; the spinner and fallback states stay content-sized — so
  // every transition is mirrored upward.
  const setPhase = (p) => { setPhaseState(p); if (onPhase) onPhase(p) }

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
      <div className="dr-no-chat-note">
        <span aria-hidden="true" className="dr-no-chat-glyph">💬</span>
        <span>
          The conversation about this brief isn’t available here. Open it from
          your chat list to reply.
        </span>
      </div>
    )
  }

  return (
    <div className="dr-chat-mount-wrap">
      {phase === 'mounting' && (
        <div className="dr-chat-resolving">
          <span className="dr-spinner dr-spinner-sm" aria-hidden="true" />
          Opening the conversation…
        </div>
      )}
      <div ref={mountRef} className="dr-chat-embed" style={{ display: phase === 'live' ? 'block' : 'none' }} />
    </div>
  )
}

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
function ReportQuestions({ questions, storage, dateStr, appId, token }) {
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
      emitSignal(appId, token, 'feedback_given', { date: dateStr, signal: 'questions' })
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
    <div className={`dr-rq${answered ? ' dr-rq--answered' : ''}`}>
      <p className="dr-rq__title">A few questions for tomorrow night</p>
      <p className="dr-rq__note">
        Your answers guide my next run — they won’t change this brief.
      </p>
      {questions.map((q, qi) => {
        const isMulti = q.multiSelect
        const cur = picks[qi]
        const selected = (label) =>
          isMulti ? (Array.isArray(cur) && cur.includes(label)) : cur === label
        return (
          <div key={qi} className="dr-rq__q">
            {q.header && <div className="dr-rq__header">{q.header}</div>}
            <div className="dr-rq__text">{q.question}</div>
            {!answered && (
              <div className="dr-rq__hint">
                {isMulti ? 'Select all that apply' : 'Choose one'}
              </div>
            )}
            <div
              className="dr-rq__opts"
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
                    className={`dr-rq__opt dr-pressable${on ? ' dr-rq__opt--on' : ''}${dim ? ' dr-rq__opt--dim' : ''}`}
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
        <div className="dr-rq__done">Saved — I’ll use this for tomorrow night’s run.</div>
      ) : (
        <>
          <button
            type="button"
            className="dr-rq__submit dr-pressable"
            onClick={submit}
            disabled={!allAnswered || saving || seeding}
          >
            {saving ? 'Saving…' : 'Save for next time'}
          </button>
          {error && <div className="dr-rq__error" role="alert">{error}</div>}
        </>
      )}
    </div>
  )
}

// The "Discuss this brief with the agent" affordance — placed at the BOTTOM of
// the scrollable content (after the brief and the morning chat), never floating
// or sticky, so it reads as the natural next step once you've finished reading.
// Mirrors the News app's feedback launcher: open the brief's own morning chat
// when one exists, else start a fresh chat seeded with a draft referencing the
// brief's date so the agent has context.
function FeedbackLauncher({ dateStr, chatId, appId, token }) {
  const openDiscussionChat = () => {
    emitSignal(appId, token, 'feedback_given', { date: dateStr })
    const draft = [
      `About the Reflection brief for ${dateStr}:`,
      '',
      '',
    ].join('\n')
    window.parent.postMessage(
      chatId
        ? { type: 'moebius:open-chat', chatId, draft }
        : { type: 'moebius:new-chat', draft },
      window.location.origin,
    )
  }
  return (
    <div className="dr-feedback-row">
      <button className="dr-feedback-btn dr-pressable" onClick={openDiscussionChat}>
        💬 Discuss this brief with the agent
      </button>
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
// the content height to the parent. The parent sizes the iframe from those
// messages so the brief reads as one scrolled column. Beneath it, the morning
// chat panel: once live it takes a fixed share of the viewport and the
// embedded ChatView scrolls internally with its composer pinned at the
// panel's bottom (see the .dr-chat-panel.is-live CSS for the height chain).
// ---------------------------------------------------------------------------

function ReportDetail({ dateStr, storage, online, onBack, appId, token }) {
  const [state, setState] = useState({ phase: 'loading', html: '' })
  // The agent's in-report questions, extracted from the RAW brief HTML and
  // rendered as native tap cards below the iframe. The carrier is stripped
  // from the HTML before hardenReportHtml so it never reaches the iframe.
  const [questions, setQuestions] = useState([])
  const [chatId, setChatId] = useState(undefined) // undefined=resolving, null=none, string=id
  const [chatPhase, setChatPhase] = useState('mounting') // mirrors MorningChat's phase
  const [briefHeight, setBriefHeight] = useState(360)
  const [reloadKey, setReloadKey] = useState(0)
  const iframeRef = useRef(null)

  // Coming back online after a failed (offline) brief load should retry the
  // body rather than stranding the reader on the offline error until they
  // navigate away and back. We bump reloadKey on the false→true transition;
  // the load effect below depends on it. (A successful brief is unaffected —
  // re-running the fetch with cache:'no-store' just re-reads the same body.)
  const wasOnline = useRef(online)
  useEffect(() => {
    if (online && !wasOnline.current) setReloadKey((k) => k + 1)
    wasOnline.current = online
  }, [online])

  // Load the brief body + resolve its chat in parallel.
  useEffect(() => {
    let cancelled = false
    setState({ phase: 'loading', html: '' })
    setQuestions([])
    setChatId(undefined)
    setChatPhase('mounting')
    setBriefHeight(360)
    ;(async () => {
      const res = await storage.getReportHtml(`${dateStr}.html`)
      if (cancelled) return
      if (res.data != null) {
        // Extract the question carrier from the RAW HTML BEFORE hardening: the
        // inert carrier <script> would otherwise ride into the sandboxed iframe
        // and the visible <section>/<h2> shell would render as raw text in the
        // brief (and the partner can't answer there). Strip it, harden the
        // remainder, render the questions natively below.
        const { html: cleaned, questions: qs } = extractReportQuestions(res.data)
        setQuestions(qs)
        setState({ phase: 'ready', html: hardenReportHtml(cleaned) })
      }
      else if (res.notFound) setState({ phase: 'missing', html: '' })
      else {
        setState({ phase: 'error', html: '' })
        emitSignal(appId, token, 'error', { message: 'brief load failed for ' + dateStr + ' (HTTP ' + res.error + ')' })
      }
    })()
    ;(async () => {
      const id = await storage.getReportChatId(dateStr)
      if (!cancelled) setChatId(id)
    })()
    return () => { cancelled = true }
  }, [dateStr, storage, reloadKey])

  // Size the brief iframe from postMessage events sent by the injected
  // height-reporter script (see hardenReportHtml + REPORT_HEIGHT_SCRIPT).
  // The iframe runs with allow-scripts but WITHOUT allow-same-origin, so
  // contentDocument is NOT readable from the parent — we receive height
  // passively via postMessage instead.
  useEffect(() => {
    const onMessage = (ev) => {
      if (!ev.data || ev.data.type !== 'reflection:brief-height') return
      // Only trust OUR brief iframe: the sandboxed frame has a null origin,
      // so ev.origin can't identify it — ev.source against the iframe's
      // contentWindow is the only way to reject spoofed height messages
      // from other windows.
      if (ev.source !== iframeRef.current?.contentWindow) return
      const h = Number(ev.data.height)
      if (Number.isFinite(h) && h > 0) {
        // The reported height is applied as-is (no buffer): the reporter
        // sends Math.ceil of an exact content metric, and re-applying a
        // buffer per emit would creep the height upward. Clamp to a sane
        // ceiling: a malformed/runaway report (broken layout, a script in
        // an infinite-growth loop) could report an enormous height and
        // grow the outer column unboundedly. 16000px is well past any real
        // one-page brief; beyond it the iframe scrolls its own overflow
        // rather than the parent column stretching forever.
        setBriefHeight(Math.min(Math.max(h, 200), 16000))
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
    <div className="dr-detail dr-rise">
      <div className="dr-detail-bar">
        <button
          className="dr-back-btn dr-pressable"
          onClick={onBack} aria-label="Back to reports"
        >
          <span aria-hidden="true" className="dr-back-glyph">‹</span> Briefs
        </button>
        <div className="dr-detail-title">
          <span className="dr-detail-title-main">{relativeLabel(dateStr)}’s brief</span>
          <span className="dr-detail-title-sub">{subLabel(dateStr)}</span>
        </div>
      </div>

      {state.phase === 'loading' && (
        <div className="dr-brief-loading">
          <span className="dr-spinner" aria-hidden="true" />
          <span>Opening your brief…</span>
        </div>
      )}

      {state.phase === 'missing' && (
        <div className="dr-empty is-compact">
          This brief is no longer available.
        </div>
      )}

      {state.phase === 'error' && (
        <div className="dr-empty is-compact">
          {online
            ? 'This brief could not be loaded. Try opening it again in a moment.'
            : 'You’re offline — open this brief again once you’re back online.'}
        </div>
      )}

      {state.phase === 'ready' && (
        <div className="dr-split-body dr-scroll">
          <div className="dr-brief-panel">
            <iframe
              ref={iframeRef}
              className="dr-brief-iframe"
              style={{ height: `${briefHeight}px` }}
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
              persist to question-answers/<date>.json for the NEXT run — no live
              agent waits (a background AskUserQuestion would park a future a
              server reset orphans). The card owns its own durable write so it
              can await the result, flip to "Saved" on a durable outcome (synced or queued), and
              re-seed the answered state from storage when the brief reopens. */}
          {questions.length > 0 && (
            <ReportQuestions
              questions={questions}
              storage={storage}
              dateStr={dateStr}
              appId={appId}
              token={token}
            />
          )}

          <div className={`dr-chat-panel${chatId && chatPhase === 'live' ? ' is-live' : ''}`}>
            <div className="dr-chat-header">
              <span className="dr-chat-header-dot" aria-hidden="true" />
              <span className="dr-chat-header-text">Morning conversation</span>
              {chatId && <span className="dr-chat-header-hint">tap a card or reply below</span>}
            </div>
            {chatId === undefined ? (
              <div className="dr-chat-resolving">
                <span className="dr-spinner dr-spinner-sm" aria-hidden="true" />
                Finding the conversation…
              </div>
            ) : chatId === null ? (
              <div className="dr-no-chat-note">
                <span aria-hidden="true" className="dr-no-chat-glyph">🌙</span>
                <span>
                  No conversation was opened for this brief — it’s a read-only
                  morning note. When a brief has questions for you, the chat
                  appears here so you can answer with a tap.
                </span>
              </div>
            ) : (
              <MorningChat chatId={chatId} onPhase={setChatPhase} />
            )}
            <FeedbackLauncher dateStr={dateStr} chatId={chatId} appId={appId} token={token} />
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

  // Live refresh: a new brief written while the app is open should appear
  // without a manual "Try again" or a full iframe reload. The cron updates
  // state.json (last_run/streak) on every overnight pass, so a change there is
  // the signal that a new brief just landed — re-list when it fires. subscribe
  // delivers the current value immediately on register; we skip that first
  // synchronous fire so we don't double-load right after the mount effect.
  useEffect(() => {
    let primed = false
    const unsub = storage.subscribeJSON('state.json', () => {
      if (!primed) { primed = true; return }
      setReloadKey((k) => k + 1)
    })
    return () => { try { unsub && unsub() } catch {} }
  }, [storage])

  // Reconnecting after an offline stretch should re-list (the offline view is
  // a frozen cached snapshot; tonight's run only appears after a fresh
  // listing). Bump reloadKey on the false→true transition.
  const wasOnline = useRef(online)
  useEffect(() => {
    if (online && !wasOnline.current) setReloadKey((k) => k + 1)
    wasOnline.current = online
  }, [online])

  if (phase === 'loading' && dates.length === 0) {
    return (
      <div className="dr-loading-wrap">
        <span className="dr-spinner" aria-hidden="true" />
        <div>Gathering last night’s brief…</div>
      </div>
    )
  }

  if (phase === 'error' && dates.length === 0) {
    return (
      <div className="dr-error-box">
        <span>
          {online
            ? 'Couldn’t load your briefs just now.'
            : 'You’re offline and there’s nothing cached yet.'}
        </span>
        {online && (
          <button className="dr-retry-btn dr-pressable" onClick={() => setReloadKey((k) => k + 1)}>
            Try again
          </button>
        )}
      </div>
    )
  }

  if (dates.length === 0) {
    return (
      <div className="dr-empty">
        <div className="dr-empty-mark">
          <span className="dr-empty-mark-glyph" aria-hidden="true">🌙</span>
        </div>
        <div className="dr-empty-title">No briefs yet</div>
        Reflection runs overnight — consolidating what the day’s agents learned,
        tidying your Memory, and tending your apps. Your first morning brief will
        be waiting right here.
      </div>
    )
  }

  return (
    <div className="dr-rise">
      <StreakBar streak={streak} />
      {!online && (
        <div className="dr-offline-banner">
          <span aria-hidden="true">🌙</span>
          Offline — showing your last cached briefs. Tonight’s brief appears
          once you’re back online.
        </div>
      )}
      <div className="dr-list">
        {dates.map((d, i) => (
          <button
            key={d}
            className={`dr-card${i === 0 ? ' is-latest' : ''}`}
            onClick={() => onOpen(d)}
          >
            <div className={`dr-date-tile${i === 0 ? ' is-latest' : ''}`} aria-hidden="true">
              <span className="dr-date-tile-day">{weekdayInitial(d)}</span>
              <span className="dr-date-tile-num">{dayOfMonth(d)}</span>
            </div>
            <div className="dr-card-main">
              <div className="dr-card-label-row">
                <span className="dr-card-label">{relativeLabel(d)}</span>
                {i === 0 && <span className="dr-latest-pill">Latest</span>}
              </div>
              <span className="dr-card-sub">{subLabel(d)}</span>
              {i === 0 && lastSummary && (
                <span className="dr-card-tldr">{lastSummary}</span>
              )}
            </div>
            <span className="dr-card-chevron" aria-hidden="true">›</span>
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
    <div className="dr-streak-bar">
      <span className="dr-streak-badge">
        <span aria-hidden="true" className="dr-streak-flame">🔥</span>
        <strong className="dr-streak-num">{streak}</strong>
        <span className="dr-streak-unit">
          {streak === 1 ? 'morning in a row' : 'mornings in a row'}
        </span>
        <span aria-hidden="true" className="dr-streak-dots">
          {'•'.repeat(flames)}
        </span>
      </span>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Last-night status row
// ---------------------------------------------------------------------------
//
// Reads GET /api/admin/activity?since=<48h> (same auth pattern as other owner
// calls in this app), filters to cron_outcome events with job=reflection, takes
// the most-recent one, and renders a compact status line.
// A failure outcome shows an "Investigate" button that posts moebius:new-chat
// with a draft asking the agent to read /data/cron-logs/reflection.log.

function LastNightStatus({ token }) {
  const [state, setState] = React.useState({ phase: 'loading', exitCode: null, ts: null })

  React.useEffect(() => {
    let cancelled = false
    ;(async () => {
      try {
        const since = new Date(Date.now() - 48 * 3600 * 1000).toISOString()
        const res = await fetch(`/api/admin/activity?since=${encodeURIComponent(since)}`, {
          headers: { Authorization: `Bearer ${token}` },
        })
        if (!res.ok) { if (!cancelled) setState({ phase: 'unavailable' }); return }
        const text = await res.text()
        if (cancelled) return
        // activity endpoint returns JSONL or a JSON array — handle both.
        const lines = text.trim().split('\n').filter(Boolean)
        const events = []
        for (const line of lines) {
          try {
            const obj = JSON.parse(line)
            // The endpoint may return a JSON array on some versions.
            if (Array.isArray(obj)) { obj.forEach((e) => events.push(e)); continue }
            events.push(obj)
          } catch { continue }
        }
        // Find the most-recent cron_outcome for reflection.
        const reflection = events
          .filter((e) => e.ev === 'cron_outcome' && e.job === 'reflection')
          .sort((a, b) => (a.ts < b.ts ? 1 : a.ts > b.ts ? -1 : 0))
        if (reflection.length === 0) {
          setState({ phase: 'none' })
        } else {
          const latest = reflection[0]
          setState({ phase: 'ready', exitCode: latest.exit_code, ts: latest.ts })
        }
      } catch {
        setState({ phase: 'unavailable' })
      }
    })()
    return () => { cancelled = true }
  }, [token])

  const investigate = () => {
    const draft = [
      'Something went wrong with the Reflection cron job. Please investigate:',
      '',
      '1. Check /data/cron-logs/reflection.log for the most recent error',
      '2. Identify the root cause (lock, timeout, config, or agent error)',
      '3. Propose a fix or next steps',
    ].join('\n')
    window.parent.postMessage({ type: 'moebius:new-chat', draft }, window.location.origin)
  }

  if (state.phase === 'loading' || state.phase === 'unavailable') return null

  const isFail = state.phase === 'ready' && Number(state.exitCode) !== 0 && Number(state.exitCode) !== 5
  const isSkip = state.phase === 'ready' && Number(state.exitCode) === 5
  const isNone = state.phase === 'none'
  const isOk   = state.phase === 'ready' && Number(state.exitCode) === 0

  const dotClass = isOk ? 'ok' : isFail ? 'fail' : isSkip ? 'skip' : 'none'
  const label = isNone
    ? 'No run recorded in the last 48 hours'
    : cronExitLabel(state.exitCode)
  const tsLabel = state.ts
    ? new Date(state.ts).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
    : ''

  return (
    <div className="dr-status-row">
      <span className={`dr-status-dot ${dotClass}`} aria-hidden="true" />
      <span className="dr-status-label">{label}</span>
      {tsLabel && <span className="dr-status-hint">{tsLabel}</span>}
      {isFail && (
        <button className="dr-status-investigate dr-pressable" onClick={investigate}>
          Investigate
        </button>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

function SettingsTab({ appId, storage, token }) {
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
      <div className="dr-loading-wrap">
        <span className="dr-spinner" aria-hidden="true" />
        <div>Loading settings…</div>
      </div>
    )
  }

  return (
    <div className="dr-settings-wrap dr-rise">
      <div className="dr-settings-card">
        <div className="dr-section-head">
          <span className="dr-section-icon" aria-hidden="true">⏰</span>
          <h2 className="dr-section-label">When it runs</h2>
        </div>
        <p className="dr-note">
          Pick the hour your morning brief should be ready. Reflection writes it
          overnight so it’s waiting when you wake.
        </p>
        {cronIsCustom ? (
          <div className="dr-custom-cron-note">
            You have a custom schedule set (<code>{rawCron}</code>). Pick an
            hour below to switch to a simple daily time, or leave it as-is.
            <div className="dr-time-row">
              <input
                type="time"
                step="3600"
                className="dr-time-input"
                value={hourToTimeValue(hour)}
                onChange={onTimeChange}
                aria-label="Daily brief time"
              />
              <span className="dr-note">on the hour, every day</span>
            </div>
          </div>
        ) : (
          <div className="dr-time-row">
            <input
              type="time"
              step="3600"
              className="dr-time-input"
              value={hourToTimeValue(hour)}
              onChange={onTimeChange}
              aria-label="Daily brief time"
            />
            <span className="dr-note">
              ready around <strong className="dr-note-strong">{hourClockLabel(hour)}</strong>, every day
            </span>
          </div>
        )}
        <div className="dr-schedule-hint">
          <span aria-hidden="true">💡</span>
          <span>
            Schedule changes take effect after the reflection agent re-installs
            its overnight job — usually by the next run. The app saves your
            preference; the agent picks it up from there.
          </span>
        </div>
      </div>

      <div className="dr-settings-card">
        <div className="dr-section-head">
          <span className="dr-section-icon" aria-hidden="true">🤖</span>
          <h2 className="dr-section-label">Nightly model</h2>
        </div>
        <p className="dr-note">
          The model Reflection uses for the overnight pass. It runs its own
          procedure with the default skill.
        </p>
        {modelGroups === null ? (
          <div className="dr-note">Loading models…</div>
        ) : modelGroups.length === 0 ? (
          // Models API unavailable — fall back to letting the CLI choose.
          <div className="dr-note">
            Model list unavailable. Reflection will use the CLI's default model
            for your account.
          </div>
        ) : (
          <>
            <select
              className="dr-select"
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
            <div className="dr-meta">
              {(modelGroups.find((group) => group.key === provider)?.label || provider)}
              {' · '}
              {model || 'provider default'}
            </div>
          </>
        )}
      </div>

      <div className="dr-settings-card">
        <div className="dr-section-head">
          <span className="dr-section-icon" aria-hidden="true">📝</span>
          <h2 className="dr-section-label">Brief style</h2>
        </div>
        <p className="dr-note">
          How long and how detailed you'd like the morning brief. The reflection
          skill honors this when writing tonight's report.
        </p>
        <div className="dr-verbosity-row">
          {VERBOSITY_OPTIONS.map((opt) => (
            <button
              key={opt.id}
              className={`dr-verb-btn${verbosity === opt.id ? ' is-active' : ''} dr-pressable`}
              onClick={() => setVerbosity(opt.id)}
              aria-pressed={verbosity === opt.id}
            >
              {opt.label}
            </button>
          ))}
        </div>
        <p className="dr-verb-hint">
          {VERBOSITY_OPTIONS.find((o) => o.id === verbosity)?.hint}
        </p>
      </div>

      <div className="dr-settings-card">
        <div className="dr-section-head">
          <span className="dr-section-icon" aria-hidden="true">🧭</span>
          <h2 className="dr-section-label">Tonight's steering</h2>
        </div>
        <p className="dr-note">
          Optional nudges the reflection agent reads before deciding what to cover.
          Leave blank to let it choose freely.
        </p>
        <label className="dr-note" style={{ display: 'block', marginBottom: 4 }}>
          <span className="dr-note-strong">Prioritise</span> — topics or apps to pay extra attention to
        </label>
        <textarea
          className="dr-textarea"
          value={focus}
          onChange={(e) => setFocus(e.target.value)}
          placeholder={'e.g. "look for regressions in the Habits app" or "I\'ve been researching climate policy"'}
          aria-label="Topics to prioritise tonight"
        />
        <label className="dr-note" style={{ display: 'block', marginTop: 10, marginBottom: 4 }}>
          <span className="dr-note-strong">Skip</span> — topics or apps to leave out of tonight's brief
        </label>
        <textarea
          className="dr-textarea"
          value={avoid}
          onChange={(e) => setAvoid(e.target.value)}
          placeholder={'e.g. "skip the workout app" or "don\'t mention work projects"'}
          aria-label="Topics to skip tonight"
        />
      </div>

      <div className="dr-save-row">
        <button className="dr-save-btn dr-pressable" onClick={save} disabled={saving}>
          {saving ? 'Saving…' : 'Save settings'}
        </button>
        {toast && <span className="dr-toast">{toast}</span>}
        {error && <span className="dr-error-toast">{error}</span>}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// App
// ---------------------------------------------------------------------------

export default function App({ appId, token }) {
  const [tab, setTab] = useState('reports')
  const [openDate, setOpenDate] = useState(null)
  const detailNavRef = useRef(null)
  const online = useOnline()
  const storage = useMemo(() => makeStorage(appId, token), [appId, token])
  const appReadyFiredRef = useRef(false)
  // A save can resolve 'queued' (durably outboxed offline) and then be FATALLY
  // refused later, when the outbox drains — an async outcome the resolved
  // promise at the call site can never carry. onDeadLetter is that out-of-band
  // channel: it fires once per such write so a "Saved" the user already saw is
  // honestly retracted here. Held at the app root because the originating
  // component (a question card, the settings form) is likely unmounted by drain
  // time. Replays unconsumed dead-letters on subscribe, so a refusal that
  // landed while the app was closed still surfaces on next open.
  const [deadLetter, setDeadLetter] = useState(null)
  useEffect(() => {
    if (!window.mobius || typeof window.mobius.onDeadLetter !== 'function') return undefined
    return window.mobius.onDeadLetter((dl) => {
      setDeadLetter(dl && dl.path === 'settings.json'
        ? 'Your schedule didn’t save — it was refused after going offline. Reopen Settings and save again.'
        : 'A queued change couldn’t be saved after you reconnected. Please try again.')
    })
  }, [])

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
      // app_ready fires once after the initial state load (whether empty or not).
      if (!appReadyFiredRef.current) {
        appReadyFiredRef.current = true
        emitSignal(appId, token, 'app_ready')
      }
    })()
    return () => { cancelled = true }
  }, [storage, appId, token])

  const closeDetail = useCallback(() => {
    try { detailNavRef.current?.close?.() } catch {}
    detailNavRef.current = null
    setOpenDate(null)
  }, [])

  const openDetail = useCallback(async (dateStr) => {
    try { detailNavRef.current?.close?.() } catch {}
    detailNavRef.current = null
    if (window.mobius?.nav?.open) {
      const handle = window.mobius.nav.open('reflection-report', () => {
        detailNavRef.current = null
        setOpenDate(null)
      })
      detailNavRef.current = handle
      await handle.ready?.catch(() => false)
      if (detailNavRef.current !== handle) return
    }
    emitSignal(appId, token, 'brief_opened', { date: dateStr })
    setOpenDate(dateStr)
  }, [appId, token])

  useEffect(() => () => {
    try { detailNavRef.current?.close?.() } catch {}
  }, [])

  return (
    <div className="dr-root">
      <style>{CSS}</style>
      <div className="dr-aurora" aria-hidden="true" />
      <div className="dr-header">
        <div className="dr-brand">
          {/* Brand mark: the app's real glossy icon (downscaled + cached),
              no name text. Falls back to an accent dot when this install
              has no custom icon and the route 404s. */}
          <img
            src={`/api/apps/${appId}/icon?size=64`}
            alt=""
            width={26}
            height={26}
            className="dr-brand-icon"
            onError={(e) => {
              e.currentTarget.style.display = 'none'
              const f = e.currentTarget.nextElementSibling
              if (f) f.style.display = 'flex'
            }}
          />
          <span className="dr-brand-fallback" style={{ display: 'none' }} aria-hidden="true">·</span>
        </div>
        <div className="dr-header-right">
          {headerStreak >= 1 && (
            <span className="dr-streak-badge" title={`${headerStreak} mornings in a row`}>
              <span aria-hidden="true">🔥</span>
              {headerStreak}
            </span>
          )}
          <div className="dr-seg" role="tablist" aria-label="View">
            <button
              role="tab"
              aria-selected={tab === 'reports'}
              className={`dr-seg-btn${tab === 'reports' ? ' is-active' : ''}`}
              onClick={() => setTab('reports')}
            >
              Briefs
            </button>
            <button
              role="tab"
              aria-selected={tab === 'settings'}
              className={`dr-seg-btn${tab === 'settings' ? ' is-active' : ''}`}
              onClick={() => { closeDetail(); setTab('settings') }}
            >
              Settings
            </button>
          </div>
        </div>
      </div>
      <div className="dr-divider" />
      <div className="dr-scroll">
        {deadLetter && (
          <div className="dr-deadletter" role="alert">
            <span>{deadLetter}</span>
            <button
              type="button"
              className="dr-deadletter__x dr-pressable"
              aria-label="Dismiss"
              onClick={() => setDeadLetter(null)}
            >
              ×
            </button>
          </div>
        )}
        {tab === 'reports' ? (
          <>
            {/* Last-night status row — shows most recent cron_outcome for reflection */}
            <LastNightStatus token={token} />
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
                appId={appId}
                token={token}
              />
            )}
          </>
        ) : (
          <SettingsTab appId={appId} storage={storage} token={token} />
        )}
      </div>
    </div>
  )
}
