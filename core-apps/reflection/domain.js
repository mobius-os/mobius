import {
  REPORT_BASE_STYLE,
  REPORT_CSP,
  REPORT_HEIGHT_SCRIPT,
} from './constants.js'

export function cronExitLabel(code) {
  const n = Number(code)
  if (n === 0) return 'ran ok'
  if (n === 5) return 'skipped (lock held)'
  if (n === 124) return 'timed out'
  if (n === 2 || n === 3) return 'config error (exit ' + n + ')'
  // The runner's own error band (>=64) — distinct from the wrapper's config
  // codes above so a model/usage/auth night is never mislabeled as config.
  if (n === 64) return 'model error'
  if (n === 65) return 'usage limit reached'
  if (n === 66) return 'provider auth expired'
  if (n === 70) return 'died before completing'
  return 'failed (exit ' + n + ')'
}

// Pull the hour out of a "0 <h> * * *" cron string. Returns null for
// anything that doesn't match the minute-0, single-hour shape this app
// writes — a hand-edited cron (e.g. "30 6 * * *" or "0 6,18 * * *")
// shouldn't be silently coerced to an hour the picker can't represent;
// the caller falls back to the default and the UI notes it can't show
// a custom schedule rather than lying about one.
export function parseCronHour(cron) {
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

export function buildCron(hour) {
  return `0 ${hour} * * *`
}

// Rough luminance test so the brief iframe's color-scheme (UA scrollbars, form
// chrome) matches a dark or light theme. Parses #rgb/#rrggbb and rgb()/rgba();
// anything unparseable is treated as light.
export function isDarkColor(c) {
  if (!c) return false
  let r, g, b
  const hex = c.trim().replace(/^#/, '')
  if (/^[0-9a-f]{3}$/i.test(hex)) {
    r = parseInt(hex[0] + hex[0], 16); g = parseInt(hex[1] + hex[1], 16); b = parseInt(hex[2] + hex[2], 16)
  } else if (/^[0-9a-f]{6}$/i.test(hex)) {
    r = parseInt(hex.slice(0, 2), 16); g = parseInt(hex.slice(2, 4), 16); b = parseInt(hex.slice(4, 6), 16)
  } else {
    const m = c.match(/rgba?\(([^)]+)\)/i)
    if (!m) return false
    ;[r, g, b] = m[1].split(',').map((x) => parseFloat(x))
  }
  if ([r, g, b].some((x) => Number.isNaN(x))) return false
  return (0.299 * r + 0.587 * g + 0.114 * b) < 128
}

// The brief renders in a NULL-ORIGIN sandboxed iframe (allow-scripts WITHOUT
// allow-same-origin, so its scripts can't reach the shell's owner JWT). A null
// origin also means the iframe does NOT inherit the app document's CSS custom
// properties — so without this, every var(--surface)/var(--text) in
// REPORT_BASE_STYLE falls back to its light literal and the brief renders
// black-on-white regardless of the active theme. Read the resolved tokens off
// the app's own :root and re-declare them inside the iframe, plus set html/body
// background+color and a luminance-matched color-scheme so the brief honors the
// theme. `rootStyle` (a CSSStyleDeclaration) is injectable for tests; in the
// app it defaults to the live document root.
export function reportThemeStyle(rootStyle) {
  const cs = rootStyle || (typeof getComputedStyle === 'function' && typeof document !== 'undefined'
    ? getComputedStyle(document.documentElement) : null)
  if (!cs) return ''
  const tokens = ['--bg', '--text', '--surface', '--surface-active', '--border',
    '--muted', '--accent', '--accent-tint', '--font']
  const decls = tokens
    .map((t) => { const v = (cs.getPropertyValue(t) || '').trim(); return v ? `${t}: ${v};` : '' })
    .filter(Boolean).join(' ')
  if (!decls) return ''
  const scheme = isDarkColor(cs.getPropertyValue('--bg')) ? 'dark' : 'light'
  return `<style>:root { ${decls} color-scheme: ${scheme}; } html, body { background: var(--bg); color: var(--text); }</style>`
}

export function hardenReportHtml(html, themeStyle = '') {
  const body = typeof html === 'string' ? html : ''
  // Order: CSP first, then the resolved theme tokens (so the null-origin iframe
  // renders in-theme instead of falling back to REPORT_BASE_STYLE's light
  // literals — see reportThemeStyle), then the base style (overflow guards +
  // details/questions defaults), then the height reporter. The base style sits
  // before the brief's own <style> so the template's richer rules win on the
  // cascade, while the html/body overflow guards (which the template never
  // sets) hold.
  const inject = `<meta http-equiv="Content-Security-Policy" content="${REPORT_CSP}">${themeStyle}${REPORT_BASE_STYLE}${REPORT_HEIGHT_SCRIPT}`
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
  // Strip the carrier by its data-report-questions attribute on ANY element,
  // not just section/div: the shell is conventionally a <section>, but a
  // carrier on aside/article/etc. must not survive into srcDoc as a stray
  // questions heading. `\1` back-refs the opening tag so the matching close is
  // removed with it.
  out = out.replace(/<([a-z][\w-]*)\b[^>]*\bdata-report-questions\b[^>]*>[\s\S]*?<\/\1>/gi, '')
  return { html: out, questions }
}

// "0 6 * * *" -> "06:00" for the <input type="time"> value.
export function hourToTimeValue(hour) {
  return `${String(hour).padStart(2, '0')}:00`
}

// A friendly clock label for the schedule summary — "6:00 AM" in the
// user's locale, so the settings header reads as plain language.
export function hourClockLabel(hour) {
  const d = new Date()
  d.setHours(hour, 0, 0, 0)
  return d.toLocaleTimeString(undefined, { hour: 'numeric', minute: '2-digit' })
}

// Date helpers — report names are <YYYY-MM-DD>.
// ---------------------------------------------------------------------------

export function todayLocalDateStr() {
  const d = new Date()
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

export function yesterdayLocalDateStr() {
  const d = new Date()
  d.setDate(d.getDate() - 1)
  const y = d.getFullYear()
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${y}-${m}-${day}`
}

// Relative label for a card: "Today" / "Yesterday" / full date.
export function relativeLabel(dateStr) {
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
export function subLabel(dateStr) {
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
export function weekdayInitial(dateStr) {
  const d = new Date(dateStr + 'T12:00:00')
  if (Number.isNaN(d.getTime())) return '·'
  return d.toLocaleDateString(undefined, { weekday: 'short' }).slice(0, 1)
}

export function dayOfMonth(dateStr) {
  const d = new Date(dateStr + 'T12:00:00')
  if (Number.isNaN(d.getTime())) return ''
  return String(d.getDate())
}

// Clamp a desired chat-pane height (px) into [pill, total - pill] and return it
// as a 0..1 ratio of the body. When the body is shorter than two pills, fall
// back to a 50/50 split so neither pane vanishes. Pure — unit-testable.
export function clampChatRatio(desiredPx, total, minPx) {
  if (!(total > 0)) return 0.5
  const floor = minPx
  const ceil = total - minPx
  if (ceil <= floor) return 0.5
  const px = Math.max(floor, Math.min(ceil, desiredPx))
  return px / total
}
