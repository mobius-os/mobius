// Shared scalar tables and report template-literal blocks.

export const VERBOSITY_OPTIONS = [
  { id: 'terse', label: 'Terse', hint: 'A short paragraph — just the highlights.' },
  { id: 'standard', label: 'Standard', hint: 'A few paragraphs, a suggestion or two, one closing thought.' },
  { id: 'chatty', label: 'Chatty', hint: 'A longer narrative with more pattern-spotting.' },
]
export const DEFAULT_VERBOSITY = 'standard'

// Exit-code meanings for cron_outcome events (from the fetch.sh legend).
// 2/3 are the WRAPPER's config errors only; the runner's own failures use a
// separate >=64 band so a model/usage/auth failure is never labeled a config
// error (the old shared codes showed a weekly usage cap as "config error").
export const CRON_EXIT_LABELS = {
  0:   'ran ok',
  2:   'config error (no app id)',
  3:   'config error (missing service token)',
  5:   'skipped — a prior run still held the lock',
  64:  'model error',
  65:  'usage limit reached',
  66:  'provider auth expired — reconnect in Settings',
  70:  'died before completing (external interruption)',
  124: 'timed out',
  127: 'runner not found',
}

export const PROVIDER_LABELS = {
  claude: 'Claude Code',
  codex: 'OpenAI Codex',
}
export const PROVIDER_ORDER = [
  { key: 'claude', label: PROVIDER_LABELS.claude },
  { key: 'codex', label: PROVIDER_LABELS.codex },
]
// When /api/auth/providers/models is unreachable we use an empty fallback
// rather than guessing model IDs. Stale hardcoded IDs would 400 the nightly
// run; letting the CLI use its own account default is always safe.
export const FALLBACK_MODEL_GROUPS = []
export const DEFAULT_PROVIDER = 'claude'
export const DEFAULT_MODEL = null

// The default schedule: 06:00 local -> "0 6 * * *". We only ever let the
// user pick an hour (minute pinned to 0), so the cron field is always
// "0 <h> * * *". parseCronHour / buildCron are the single round-trip
// between the picker's hour and that string.
export const DEFAULT_HOUR = 6
export const DEFAULT_CRON = '0 6 * * *'

export const REPORT_CSP = [
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
export const REPORT_BASE_STYLE = `<style>
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
    border: 1px solid color-mix(in srgb, var(--accent, #6d5ef0) 40%, var(--border, #e4e1dc));
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

  /* ---- Lean-brief structure (mobius-ui:BriefStyle v1) ----
     Recent Reflection briefs are LEAN semantic HTML with no <style> of their
     own; the app owns their layout here (older self-contained briefs carry their
     own <style>, injected AFTER this, so they still win the cascade). Themed via
     the vars reportThemeStyle injects, with literal fallbacks so a missing token
     degrades gracefully. Spacing/type/radius tokens are theme-independent, so
     they are declared here rather than read off the shell. */
  :root {
    --sp-1: .25rem; --sp-2: .5rem; --sp-3: .75rem; --sp-4: 1rem;
    --sp-5: 1.5rem; --sp-6: 2rem; --sp-7: 3rem;
    --step--1: .86rem; --step-0: 1rem; --step-1: 1.2rem; --step-2: 1.44rem;
    --radius: 14px; --radius-sm: 9px; --report-maxw: 46rem;
  }
  body {
    font-family: var(--font, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif);
    font-size: var(--step-0); line-height: 1.6; color: var(--text, #1c1b19);
    -webkit-font-smoothing: antialiased;
  }
  .brief {
    width: min(100%, var(--report-maxw)); margin: 0 auto;
    padding: clamp(var(--sp-4), 4vw, var(--sp-6)) var(--sp-4) var(--sp-7);
    overflow-wrap: anywhere;
  }
  .brief section { margin: var(--sp-6) 0; }
  .brief section:first-child { margin-top: 0; }
  .brief h2 { margin: 0 0 var(--sp-3); font-size: var(--step-2); line-height: 1.2; font-weight: 640; color: var(--text, #1c1b19); }
  .brief p { margin: var(--sp-2) 0; }
  .brief ul, .brief ol { margin: var(--sp-3) 0 0; padding-left: 1.35em; }
  .brief li { margin: var(--sp-2) 0; line-height: 1.55; }
  .brief strong { font-weight: 640; }

  /* Summary card (.lede / .headline / .keypoints). */
  .brief .lede {
    padding: var(--sp-5); border: 1px solid var(--border, #e4e1dc);
    border-radius: var(--radius); background: var(--surface, #fff);
    box-shadow: 0 1px 2px rgba(0,0,0,.12), 0 4px 8px rgba(0,0,0,.14);
  }
  .brief .headline { margin: 0; font-size: var(--step-1); line-height: 1.45; font-weight: 560; color: var(--text, #1c1b19); }
  .brief .keypoints { list-style: none; margin: var(--sp-4) 0 0; padding-left: 0; }
  .brief .keypoints li { position: relative; margin: 0; padding: var(--sp-2) 0 var(--sp-2) var(--sp-5); border-top: 1px solid var(--border, #e4e1dc); }
  .brief .keypoints li:first-child { border-top: 0; }
  .brief .keypoints li::before { content: ""; position: absolute; left: var(--sp-1); top: 1.15em; width: 7px; height: 7px; border-radius: 50%; background: var(--accent, #6d5ef0); }

  /* Numbered section header (.sec-head + .sec-num badge). */
  .brief .sec-head { display: flex; align-items: center; gap: var(--sp-3); margin-bottom: var(--sp-4); }
  .brief .sec-head h2 { margin: 0; }
  .brief .sec-num {
    flex: none; min-width: 1.6em; height: 1.6em; padding: 0 .4em;
    display: inline-grid; place-items: center; border-radius: 999px;
    color: var(--accent, #6d5ef0);
    background: var(--accent-tint, color-mix(in srgb, var(--accent, #6d5ef0) 18%, transparent));
    font-size: var(--step--1); font-weight: 700; font-variant-numeric: tabular-nums; line-height: 1;
  }

  /* Item cards (details.item) + their meta rows (<dl class="meta"><div><dt>k</dt><span class="v">…). */
  .brief .items { display: grid; gap: var(--sp-4); }
  .brief .items > details { margin: 0; }
  .brief .item p.lead, .brief p.lead { margin-top: 0; }
  .brief .meta { display: grid; gap: var(--sp-2); margin: var(--sp-3) 0 0; }
  .brief .meta > div { display: grid; grid-template-columns: minmax(4.5rem, 6rem) 1fr; gap: var(--sp-3); align-items: baseline; font-size: var(--step--1); }
  .brief .meta dt, .brief .meta .k { margin: 0; color: var(--muted, #6b6862); letter-spacing: 0; font-size: .72rem; font-weight: 650; }
  .brief .meta .v { color: var(--text, #1c1b19); }
  .brief .meta .v.action { color: var(--accent, #6d5ef0); font-weight: 560; }

  /* Status badges — color-mix tint keeps them theme-agnostic. */
  .brief .badge {
    display: inline-block; vertical-align: baseline; white-space: nowrap;
    font-size: .7rem; font-weight: 650; letter-spacing: 0;
    padding: .18em .6em; border-radius: 999px;
    color: var(--accent, #6d5ef0); background: color-mix(in srgb, currentColor 16%, transparent);
  }
  .brief .badge.done, .brief .badge.fixed { color: var(--green, #3fb984); }
  .brief .badge.hold, .brief .badge.review { color: var(--amber, #d29a3a); }
  .brief .badge.risk { color: var(--danger, #e26a63); }

  /* /mobius-ui:BriefStyle */
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
export const REPORT_HEIGHT_SCRIPT = `<script>
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

// ---------------------------------------------------------------------------
// Chat-split sizing — mirrors app-latex / app-webstudio so the chat reads the
// same across apps. chatOpen: the chat panel is visible (the report read takes
// the top, the chat the bottom). chatRatio: 0..1 fraction of the detail-body
// height the chat panel occupies. Both persist per-app in localStorage.
// ---------------------------------------------------------------------------
export const CHAT_OPEN_VERSION = 1
export const CHAT_RATIO_VERSION = 1
// Floor the chat pane at the embedded composer pill (~64px) + the divider
// (10px) so the input is never clipped; the same floor caps the OTHER end so
// the report read never fully eats the chat.
export const CHAT_PILL_MIN_PX = 64
export const CHAT_DIVIDER_PX = 10
export const CHAT_PANE_MIN_PX = CHAT_PILL_MIN_PX + CHAT_DIVIDER_PX
