import { useState, useEffect, useRef, useMemo } from 'react'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import {
  parseSkill,
  classifyLink,
  friendlyLoadError,
  createDetailNav,
  createSystemPromptAppsLoader,
  installedAppDisplayName,
  skillContentPath,
  provenanceChip,
  isUninstallable,
  skillDisplayTitle,
  usageLabel,
} from './domain.js'
import {
  DEFAULT_SOURCES,
  sourceKey,
  treeToSkills,
  catalogSummary,
  treeScanUrl,
  rawSkillUrl,
  githubSkillUrl,
  createSummaryPrefetcher,
} from './catalog.js'

// Skills — browse, read, and grow the agent's skills (the SKILL-style markdown
// under /data/shared/skills). Based on the upstream mobius-os/app-skills
// read-only browser (v1.1.2, MIT); this version adds the write half:
//
//   - the list comes from GET /api/skills, so directory-shaped skills
//     (<name>/SKILL.md) appear too, with provenance + 30-day usage,
//   - a Find button opens an agent chat (moebius:new-chat draft) — the
//     finding-skills seed skill is the agent's discovery playbook,
//   - a catalog screen scans public skill repos (one git-trees call per source
//     through /api/proxy) and installs via POST /api/skills/install,
//   - install-provenance skills can be removed from the detail view.
//
// Creating/editing a skill still routes to the agent — a mini-app can read
// shared storage but not write it; install/uninstall go through the skills API
// (gated by permissions.manage_skills). Pure logic lives in ./domain.js and
// ./catalog.js so it stays unit-testable without react/marked/dompurify.

const CSS = `
/* mobius-ui:Root v1 — keep in sync; library candidate. */
.sk-root { position: relative; display: flex; flex-direction: column; height: 100%;
  width: 100%; max-width: 100%; overflow: hidden; background: var(--bg); color: var(--text); font-family: var(--font);
  -webkit-font-smoothing: antialiased; -webkit-tap-highlight-color: transparent; }
.sk-scroll { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; -webkit-overflow-scrolling: touch; }
/* /mobius-ui:Root */

/* mobius-ui:Page — app-owned; a future-library candidate (no sync owed).
   Reading column: the scroll owns the full-bleed scrollbar; this caps the CONTENT.
   Full-bleed on phones, centered at 720px (matches the .sk-md detail cap) on wide
   viewports so list and detail agree. */
.sk-page { width: 100%; }
@media (min-width: 760px) { .sk-page { max-width: 720px; margin-inline: auto; } }
/* /mobius-ui:Page */

/* mobius-ui:Scrollskin v2 — keep in sync; hidden by default, content stays scrollable. */
.sk-scroll,
.sk-md pre,
.sk-md table {
  scrollbar-width: none;
  -ms-overflow-style: none;
}
.sk-scroll::-webkit-scrollbar,
.sk-md pre::-webkit-scrollbar,
.sk-md table::-webkit-scrollbar {
  display: none;
  width: 0;
  height: 0;
}
/* /mobius-ui:Scrollskin */

/* mobius-ui:Header v1 — keep in sync; library candidate. */
.sk-header { flex: 0 0 auto; display: flex; align-items: center; gap: 12px; min-height: 48px;
  padding: max(12px, env(safe-area-inset-top)) 16px 12px; background: var(--surface); border-bottom: 1px solid var(--border); }
.sk-brand { display: flex; align-items: center; gap: 11px; min-width: 0; flex: 1; }
.sk-mark { flex: 0 0 auto; width: 34px; height: 34px; border-radius: 8px; display: flex;
  align-items: center; justify-content: center; overflow: hidden; color: var(--accent); }
.sk-mark img { width: 100%; height: 100%; border-radius: inherit; object-fit: cover; display: block; }
.sk-mark-fallback { width: 34px; height: 34px; border-radius: 8px; display: none;
  align-items: center; justify-content: center; font-size: 32px; font-weight: 700; line-height: 1;
  background: color-mix(in srgb, var(--accent) 14%, transparent); color: var(--accent); }
.sk-title { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: 0; }
.sk-subtitle { display: block; margin-top: 1px; font-size: 12px; color: var(--muted); }
.sk-iconbtn { position: relative; flex: 0 0 auto; width: 44px; height: 44px; display: inline-flex; align-items: center;
  justify-content: center; border-radius: 10px; border: 1px solid var(--border); background: var(--surface);
  color: var(--text); cursor: pointer; transition: background .14s ease, transform .1s ease; }
/* custom tooltip — faster than the native title (~0.15s vs ~1s) and stylable */
.sk-tip { position: absolute; top: calc(100% + 6px); right: 0; z-index: 30; pointer-events: none;
  background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 8px;
  padding: 5px 10px; font-size: 12px; font-weight: 400; line-height: 1.35; white-space: nowrap;
  box-shadow: 0 4px 14px rgba(0, 0, 0, 0.18); opacity: 0; transform: translateY(-2px);
  transition: opacity .12s ease .15s, transform .12s ease .15s; }
.sk-iconbtn:hover .sk-tip,
.sk-iconbtn:focus-visible .sk-tip { opacity: 1; transform: none; }
.sk-iconbtn:active { transform: scale(0.94); }
.sk-iconbtn:disabled { opacity: 0.5; cursor: default; }
.sk-iconbtn svg { width: 18px; height: 18px; }
.sk-iconbtn.is-spinning svg { animation: sk-spin 0.9s linear infinite; }
.sk-iconbtn.is-armed { border-color: var(--danger); color: var(--danger); }
@keyframes sk-spin { to { transform: rotate(360deg); } }
/* /mobius-ui:Header */

/* search */
.sk-searchwrap { position: sticky; top: 0; z-index: 5; padding: 12px 16px 8px; background: var(--bg); }
.sk-search { position: relative; display: flex; align-items: center; }
.sk-search svg { position: absolute; left: 12px; width: 17px; height: 17px; color: var(--muted); pointer-events: none; }
.sk-input { width: 100%; box-sizing: border-box; min-height: 44px; padding: 11px 14px 11px 38px;
  background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 10px;
  outline: none; font-family: var(--font); font-size: 16px;
  transition: border-color .15s ease, box-shadow .15s ease; }
.sk-input:focus,
.sk-input:focus-visible { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-dim); }

/* list */
.sk-list { display: flex; flex-direction: column; padding: 4px 12px 32px; }
.sk-row { display: flex; align-items: flex-start; gap: 13px; width: 100%; box-sizing: border-box;
  text-align: left; padding: 14px 8px; background: none; border: none; border-bottom: 1px solid var(--border-light, var(--border));
  color: var(--text); font-family: var(--font); cursor: pointer; }
.sk-row:last-child { border-bottom: none; }
.sk-row:active { background: color-mix(in srgb, var(--text) 5%, transparent); }
.sk-rowicon { flex: 0 0 auto; width: 40px; height: 40px; border-radius: 20px; display: flex;
  align-items: center; justify-content: center; font-size: 18px;
  background: color-mix(in srgb, var(--accent) 12%, transparent); }
.sk-rowbody { flex: 1; min-width: 0; }
.sk-rowname { font-size: 16px; font-weight: 650; letter-spacing: 0; word-break: break-word; }
.sk-rowslug { font-size: 12px; color: var(--muted); font-family: var(--mono); margin-top: 1px; }
.sk-rowdesc { margin-top: 4px; font-size: 13.5px; line-height: 1.5; color: var(--muted);
  display: -webkit-box; -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
.sk-rowtag { display: inline-block; margin-top: 5px; font-size: 11px; font-weight: 600; color: var(--danger);
  padding: 1px 7px; border-radius: 999px; border: 1px solid var(--danger); }
.sk-chev { flex: 0 0 auto; align-self: center; color: var(--muted); opacity: 0.6; }
.sk-chev svg { width: 18px; height: 18px; }


/* provenance + usage chips (rows and detail) */
.sk-provrow { display: flex; align-items: center; gap: 7px; margin-top: 6px; flex-wrap: wrap; min-width: 0; }
.sk-prov { font-size: 11px; font-weight: 600; padding: 1px 8px; border-radius: 999px;
  border: 1px solid var(--border); color: var(--muted); max-width: 100%;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sk-prov.seed { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 50%, transparent); }
.sk-prov.installed { color: var(--success, #2e9e5b); border-color: color-mix(in srgb, var(--success, #2e9e5b) 55%, transparent); }
.sk-uses { font-size: 11px; color: var(--muted); white-space: nowrap; }
.sk-detailmeta { display: flex; align-items: center; gap: 7px; flex-wrap: wrap;
  max-width: 720px; margin: 0 auto; padding: 14px 18px 0; }

/* installed apps that contribute read-only, always-on prompt context */
.sk-system-apps { margin: 0 20px; padding: 24px 0 max(32px, env(safe-area-inset-bottom));
  border-top: 1px solid var(--border); }
.sk-section-title { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: 0; text-wrap: balance; }
.sk-section-copy { margin: 6px 0 0; max-width: 68ch; color: var(--muted); font-size: 13.5px; line-height: 1.5;
  text-wrap: pretty; }
.sk-app-list { display: flex; flex-direction: column; margin-top: 12px; }
.sk-app-row { display: flex; align-items: center; gap: 13px; min-height: 44px; padding: 10px 0;
  border-bottom: 1px solid var(--border-light, var(--border)); }
.sk-app-row:last-child { border-bottom: none; }
.sk-app-icon { flex: 0 0 auto; width: 40px; height: 40px; border-radius: 10px; overflow: hidden;
  background: color-mix(in srgb, var(--accent) 12%, transparent); }
.sk-app-icon img { display: block; width: 100%; height: 100%; object-fit: cover; }
.sk-app-icon-fallback { display: none; width: 100%; height: 100%; align-items: center; justify-content: center;
  color: var(--accent); font-size: 17px; font-weight: 700; }
.sk-app-note { margin-top: 3px; color: var(--muted); font-size: 13.5px; line-height: 1.4;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden; }

/* empty / status */
.sk-empty { display: flex; flex-direction: column; align-items: center; text-align: center; gap: 8px;
  margin: auto; padding: 56px 28px; color: var(--muted); }
.sk-empty-mark { width: 64px; height: 64px; margin-bottom: 8px; border-radius: 18px; display: flex;
  align-items: center; justify-content: center; font-size: 30px;
  background: color-mix(in srgb, var(--accent) 14%, transparent); }
.sk-empty-title { font-size: 17px; font-weight: 700; color: var(--text); }
.sk-empty-text { margin: 0; font-size: 14px; line-height: 1.6; max-width: 30ch; }
.sk-spinner { width: 26px; height: 26px; border-radius: 50%; border: 2.5px solid var(--border);
  border-top-color: var(--accent); animation: sk-spin 0.8s linear infinite; }
.sk-retry { margin-top: 6px; min-height: 44px; padding: 10px 18px; border-radius: 10px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text); font-weight: 500; font-size: 14px; cursor: pointer; }

/* mobius-ui:SyncPill v2 — keep in sync; library candidate. SILENT WHEN HEALTHY:
   not mounted while online (never "Saving" / pending counts); plain "Offline"
   when offline; .is-error only for a failure the owner can act on. */
.sk-sync-pill { position: absolute; right: 12px; bottom: 12px; z-index: 40;
  display: inline-flex; align-items: center; padding: 6px 12px; border-radius: 999px;
  background: var(--surface); border: 1px solid var(--border); color: var(--muted);
  font-size: 11px; font-weight: 600; box-shadow: 0 2px 8px rgba(0,0,0,0.18); }
.sk-sync-pill.is-error { border-color: var(--danger); color: var(--danger); }
/* /mobius-ui:SyncPill */

/* detail */
.sk-detail-head { position: sticky; top: 0; z-index: 5; display: flex; align-items: center; gap: 10px;
  padding: max(12px, env(safe-area-inset-top)) 12px 12px; background: var(--surface); border-bottom: 1px solid var(--border); }
.sk-back { flex: 0 0 auto; display: inline-flex; align-items: center; gap: 4px; min-height: 44px; padding: 8px 12px 8px 8px;
  border-radius: 10px; border: none; background: none; color: var(--accent); font-family: var(--font);
  font-size: 15px; font-weight: 500; cursor: pointer; }
.sk-back svg { width: 20px; height: 20px; }
.sk-detail-title { font-size: 16px; font-weight: 700; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; flex: 1; }
.sk-md { padding: 18px 18px 48px; font-size: 15px; line-height: 1.65; max-width: 720px; margin: 0 auto; }
.sk-md h1 { font-size: 22px; font-weight: 750; letter-spacing: 0; margin: 4px 0 12px; }
.sk-md h2 { font-size: 18px; font-weight: 700; margin: 26px 0 10px; padding-top: 6px; border-top: 1px solid var(--border-light, var(--border)); }
.sk-md h3 { font-size: 15.5px; font-weight: 700; margin: 20px 0 8px; }
.sk-md p { margin: 0 0 12px; }
.sk-md ul, .sk-md ol { margin: 0 0 12px; padding-left: 22px; }
.sk-md li { margin: 4px 0; }
.sk-md a { color: var(--accent); text-decoration: none; }
.sk-md code { font-family: var(--mono); font-size: 0.86em; background: color-mix(in srgb, var(--text) 8%, transparent);
  padding: 1px 5px; border-radius: 5px; word-break: break-word; }
.sk-md pre { background: var(--surface2, var(--surface)); border: 1px solid var(--border); border-radius: 10px;
  padding: 12px 14px; overflow-x: auto; margin: 0 0 14px; }
.sk-md pre code { background: none; padding: 0; font-size: 12.5px; line-height: 1.55; }
.sk-md blockquote { margin: 0 0 12px; padding: 10px 14px; border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border));
  border-radius: 8px; background: color-mix(in srgb, var(--accent) 9%, transparent); color: var(--muted); }
.sk-md table { border-collapse: collapse; width: 100%; margin: 0 0 14px; font-size: 13.5px; display: block; overflow-x: auto; }
.sk-md th, .sk-md td { border: 1px solid var(--border); padding: 7px 10px; text-align: left; }
.sk-md th { background: color-mix(in srgb, var(--text) 5%, transparent); font-weight: 650; }
.sk-md hr { border: none; border-top: 1px solid var(--border); margin: 20px 0; }
.sk-md img { max-width: 100%; }

/* alerts (catalog + detail actions) */
.sk-alert { flex: 0 0 auto; margin: 10px 16px 0; padding: 9px 12px; border-radius: 10px; font-size: 13px;
  line-height: 1.45; border: 1px solid var(--border); color: var(--muted); background: var(--surface); }
.sk-alert.is-error { border-color: var(--danger); color: var(--danger); white-space: pre-wrap; }

/* catalog screen (overlay over the list, so list state/scroll survive) */
.sk-cat { position: absolute; inset: 0; z-index: 10; display: flex; flex-direction: column; background: var(--bg); }
.sk-cat-note { margin: 12px 20px 4px; max-width: 68ch; color: var(--muted); font-size: 13.5px; line-height: 1.5;
  text-wrap: pretty; }
/* catalog breadcrumb chain — every ancestor segment is clickable */
.sk-crumbs { flex: 1; min-width: 0; display: flex; align-items: center; gap: 6px; overflow: hidden; }
.sk-crumb { border: none; background: none; padding: 0; font-family: var(--font); font-size: 14.5px;
  color: var(--accent); cursor: pointer; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.sk-crumb.cur { color: var(--text); font-weight: 700; cursor: default; }
.sk-crumbsep { flex: 0 0 auto; color: var(--muted); }
.sk-cat-count { padding: 2px 16px 8px; font-size: 12.5px; color: var(--muted); }
.sk-cards { padding: 0 12px 32px; }
.sk-card { border: 1px solid var(--border); border-radius: 12px; background: var(--surface);
  padding: 12px 14px; margin-bottom: 10px; cursor: pointer; }
.sk-card h3 { margin: 0 0 4px; font-size: 15px; font-weight: 650; display: flex; align-items: center;
  gap: 8px; flex-wrap: wrap; word-break: break-word; }
.sk-carddesc { margin: 0 0 10px; font-size: 13.5px; color: var(--muted); line-height: 1.5; }
.sk-cardbtns { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.sk-btn { min-height: 40px; padding: 8px 16px; border-radius: 10px; border: 1px solid var(--accent);
  background: var(--accent); color: var(--accent-fg, #fff); font-family: var(--font); font-size: 13.5px;
  font-weight: 600; cursor: pointer; }
.sk-btn:disabled { opacity: 0.5; cursor: default; }
.sk-btn.ghost { background: none; color: var(--accent); text-decoration: none; display: inline-flex;
  align-items: center; gap: 6px; }
.sk-btn.ghost svg { width: 15px; height: 15px; }

/* mobius-ui:Focus v1 — keep in sync; library candidate. Required once per app.
   A visible keyboard-focus ring on every interactive control (WCAG 2.4.7).
   :focus-visible only shows for keyboard nav, so mouse/touch taps stay clean. */
:where(button, a, input, textarea, select, summary, [role="button"],
       [tabindex]:not([tabindex="-1"])):focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
/* /mobius-ui:Focus */

/* mobius-ui:ReducedMotion v1 — keep in sync; library candidate. Required once per app. */
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

const HAMMER = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m15 12-8.5 8.5a2.12 2.12 0 1 1-3-3L12 9"/><path d="M17.64 15 22 10.64"/><path d="m20.91 11.7-1.25-1.25c-.6-.6-.93-1.4-.93-2.25v-.86L16.01 4.6a5.56 5.56 0 0 0-3.94-1.64H9l.92.82A6.18 6.18 0 0 1 12 8.4v1.56l2 2h.86c.85 0 1.65.34 2.25.93l1.25 1.25"/></svg>
const REFRESH = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/><path d="M8 16H3v5"/></svg>
const SEARCH = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
const CHEV = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6"/></svg>
const BACK = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m15 18-6-6 6-6"/></svg>
const PLUS = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M5 12h14"/><path d="M12 5v14"/></svg>
const SPARKLE = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m12 3-1.9 5.8a2 2 0 0 1-1.3 1.3L3 12l5.8 1.9a2 2 0 0 1 1.3 1.3L12 21l1.9-5.8a2 2 0 0 1 1.3-1.3L21 12l-5.8-1.9a2 2 0 0 1-1.3-1.3L12 3Z"/></svg>
const BOOK = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/></svg>
const TRASH = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6"/><path d="M8 6V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
const EXTERNAL = <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M15 3h6v6"/><path d="M10 14 21 3"/><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/></svg>

// Prefilled draft for the Find flow. The agent's playbook is the
// finding-skills seed skill (sources, fit criteria, the trust ritual, and the
// exact install call); the owner just finishes this sentence.
const FIND_DRAFT = "I want to find and install a new skill for my agent. Here's what I'm trying to do: "

function initialOnline() {
  if (typeof window !== 'undefined' && typeof window.mobius?.online === 'boolean') return window.mobius.online
  if (typeof navigator !== 'undefined') return navigator.onLine
  return true
}

function ProvChips({ provenance, uses }) {
  const chip = provenanceChip(provenance)
  const usage = usageLabel(uses)
  return (
    <span className="sk-provrow">
      <span className={`sk-prov ${chip.kind}`} title={chip.title}>{chip.label}</span>
      {usage && <span className="sk-uses">{usage}</span>}
    </span>
  )
}

// One catalog card. Summaries prefetch in the background after the source
// scan; the IntersectionObserver only lets visible cards jump that queue (and
// is the fallback when the pool is cancelled mid-run). Tapping a card opens
// the full SKILL.md as its own page, like an installed skill.
function CatalogCard({ skill, desc, installed, busy, onOpen, onLoad, onInstall }) {
  const ref = useRef(null)

  useEffect(() => {
    if (desc || !ref.current || typeof IntersectionObserver === 'undefined') return undefined
    const obs = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) { onLoad(); obs.disconnect() }
    }, { rootMargin: '250px' })
    obs.observe(ref.current)
    return () => obs.disconnect()
  }, [desc])

  const loaded = desc && desc !== 'loading' && desc !== 'failed'
  return (
    <div ref={ref} className="sk-card" onClick={onOpen}>
      <h3>
        {skill.name}
        {installed && <span className="sk-prov installed">installed</span>}
      </h3>
      <p className="sk-carddesc">
        {loaded ? desc.description
          : desc === 'failed' ? 'Could not load SKILL.md.'
            : 'Loading summary…'}
      </p>
      <div className="sk-cardbtns">
        <button
          className="sk-btn"
          disabled={busy || installed}
          onClick={(e) => { e.stopPropagation(); onInstall() }}
          title={installed ? 'Already in your agent’s skills' : 'Install this skill for your agent'}
        >
          {installed ? 'Installed' : busy ? 'Installing…' : 'Install'}
        </button>
      </div>
    </div>
  )
}

// The catalog screen: curated sources → one git-trees scan each → flat cards.
// Rendered as a hidden-not-unmounted overlay so scan results and scroll
// survive closing and reopening it.
function CatalogScreen({ visible, authHeaders, existingIds, onInstalled, onClose }) {
  const [sources, setSources] = useState(DEFAULT_SOURCES)
  const [open, setOpen] = useState(null) // { source } | null = source list
  const [skillList, setSkillList] = useState(null)
  const [truncated, setTruncated] = useState(false)
  const [descs, setDescs] = useState({}) // dir -> { ...summary, raw } | 'loading' | 'failed'
  const [filter, setFilter] = useState('')
  const [detailDir, setDetailDir] = useState(null) // dir open as a full page
  const [busyDir, setBusyDir] = useState(null)
  const [scanBusy, setScanBusy] = useState(false)
  const [error, setError] = useState(null)
  const [notice, setNotice] = useState(null)
  const descsRef = useRef(descs)
  descsRef.current = descs
  const inflightRef = useRef(new Set()) // synchronous dedupe (descs lags a render)
  const prefetcherRef = useRef(null)

  useEffect(() => {
    // Sources are app data: a saved sources.json overrides the defaults, so
    // "add this repo as a source" is a chat request, not a code change.
    const storage = window.mobius?.storage
    if (storage && typeof storage.get === 'function') {
      Promise.resolve(storage.get('sources.json'))
        .then((saved) => { if (Array.isArray(saved) && saved.length) setSources(saved) })
        .catch(() => {})
    }
    return () => prefetcherRef.current?.cancel()
  }, [])

  const proxied = async (url) => {
    const res = await fetch(`/api/proxy?url=${encodeURIComponent(url)}`, { headers: authHeaders })
    if (!res.ok) throw new Error(`fetch failed (${res.status}) for ${url}`)
    return res.text()
  }

  const loadDescription = async (source, dir) => {
    if (descsRef.current[dir] || inflightRef.current.has(dir)) return
    inflightRef.current.add(dir)
    setDescs((d) => ({ ...d, [dir]: 'loading' }))
    try {
      // Keep the raw markdown next to the summary: the card only needs the
      // description, but the full-page detail view renders the whole file.
      const text = await proxied(rawSkillUrl(source, dir))
      setDescs((d) => ({ ...d, [dir]: { ...catalogSummary(text), raw: text } }))
    } catch {
      setDescs((d) => ({ ...d, [dir]: 'failed' }))
    }
  }

  const openSource = async (source) => {
    prefetcherRef.current?.cancel()
    setOpen({ source })
    setSkillList(null); setTruncated(false); setDescs({}); setFilter(''); setDetailDir(null)
    setScanBusy(true); setError(null); setNotice(null)
    inflightRef.current = new Set()
    try {
      const data = JSON.parse(await proxied(treeScanUrl(source)))
      if (!Array.isArray(data.tree)) throw new Error(data.message || 'unexpected GitHub response (no tree)')
      const skills = treeToSkills(data.tree, source.path)
      setSkillList(skills)
      setTruncated(!!data.truncated)
      window.mobius?.signal?.('item_opened', { type: 'catalog-source', slug: source.repo })
      const prefetcher = createSummaryPrefetcher({ loadOne: (dir) => loadDescription(source, dir) })
      prefetcherRef.current = prefetcher
      prefetcher.start(skills.map((s) => s.dir))
    } catch (e) {
      setError(String(e?.message || e))
      window.mobius?.signal?.('error', { message: String(e?.message || e), source: 'catalog_scan' })
    } finally {
      setScanBusy(false)
    }
  }

  const backToSources = () => {
    prefetcherRef.current?.cancel()
    setOpen(null)
    setSkillList(null); setError(null); setNotice(null); setDetailDir(null); setFilter('')
  }

  const openSkillPage = (dir) => {
    setDetailDir(dir)
    if (open) loadDescription(open.source, dir)
    window.mobius?.signal?.('item_opened', { type: 'catalog-skill', slug: dir })
  }

  const install = async (source, dir) => {
    setBusyDir(dir); setError(null); setNotice(null)
    try {
      const res = await fetch('/api/skills/install', {
        method: 'POST',
        headers: { ...authHeaders, 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo: source.repo, path: dir, ref: source.ref || 'main' }),
      })
      const data = await res.json().catch(() => ({}))
      if (!res.ok) throw new Error(data.detail || `install failed (${res.status})`)
      setNotice(`Installed "${data.name}" — it's in your agent's skills now.`)
      window.mobius?.signal?.('skill_installed', { slug: data.name, source: source.repo })
      onInstalled()
    } catch (e) {
      setError(String(e?.message || e))
      window.mobius?.signal?.('error', { message: String(e?.message || e), source: 'skill_install' })
    } finally {
      setBusyDir(null)
    }
  }

  const shown = useMemo(() => {
    if (!skillList) return null
    const q = filter.trim().toLowerCase()
    if (!q) return skillList
    return skillList.filter((s) => s.dir.toLowerCase().includes(q))
  }, [skillList, filter])

  const detailName = detailDir ? detailDir.split('/').pop() : null
  const detailInstalled = detailName ? existingIds.has(detailName) : false
  const detailEntry = detailDir ? descs[detailDir] : null
  const detailLoaded = detailEntry && detailEntry !== 'loading' && detailEntry !== 'failed'
  const detailHtml = useMemo(() => {
    if (!detailLoaded) return ''
    try {
      return DOMPurify.sanitize(marked.parse(parseSkill('SKILL.md', detailEntry.raw || '').content || ''))
    } catch (err) {
      window.mobius?.signal?.('error', { message: String(err?.message || err), source: 'markdown_render' })
      return ''
    }
  }, [detailEntry])

  // Links in a catalog SKILL.md: external → new tab; anything else (relative
  // resource paths we haven't fetched) is blocked so the app stays mounted.
  function onDetailClick(e) {
    const a = e.target.closest && e.target.closest('a')
    if (!a) return
    const link = classifyLink(a.getAttribute('href'))
    if (link.kind === 'anchor') return
    e.preventDefault()
    if (link.kind === 'external') window.open(link.url, '_blank', 'noopener,noreferrer')
  }

  return (
    <div className="sk-cat" style={visible ? undefined : { display: 'none' }} aria-hidden={!visible}>
      <div className="sk-detail-head">
        <button className="sk-back" onClick={onClose} aria-label="Back to skills">
          {BACK}<span>Skills</span>
        </button>
        {/* Full breadcrumb chain — tap any ancestor to jump straight back to it. */}
        <nav className="sk-crumbs" aria-label="Catalog navigation">
          {open ? (
            <button className="sk-crumb" onClick={backToSources}>Skill catalogs</button>
          ) : (
            <span className="sk-crumb cur">Skill catalogs</span>
          )}
          {open && <span className="sk-crumbsep" aria-hidden="true">›</span>}
          {open && (detailDir ? (
            <button className="sk-crumb" onClick={() => setDetailDir(null)}>{open.source.label}</button>
          ) : (
            <span className="sk-crumb cur">{open.source.label}</span>
          ))}
          {detailDir && <span className="sk-crumbsep" aria-hidden="true">›</span>}
          {detailDir && <span className="sk-crumb cur">{detailName}</span>}
        </nav>
        {detailDir && open && (
          <>
            <button
              className="sk-btn"
              disabled={busyDir === detailDir || detailInstalled}
              onClick={() => install(open.source, detailDir)}
              title={detailInstalled ? 'Already in your agent’s skills' : 'Install this skill for your agent'}
            >
              {detailInstalled ? 'Installed' : busyDir === detailDir ? 'Installing…' : 'Install'}
            </button>
            <a
              className="sk-iconbtn"
              href={githubSkillUrl(open.source, detailDir)}
              target="_blank"
              rel="noopener noreferrer"
              aria-label="View on GitHub"
              title="View on GitHub"
            >{EXTERNAL}</a>
          </>
        )}
      </div>
      {error && <div className="sk-alert is-error" role="alert">{error}</div>}
      {notice && !error && <div className="sk-alert" role="status">{notice}</div>}
      <div className="sk-scroll">
        <div className="sk-page">
          {!open && (
            <>
              <p className="sk-cat-note">
                Public catalogs that host installable skills. Open one to see every skill it
                holds, or use ✦ Find on the main screen to have the agent search them all —
                the agent also covers community awesome-lists and the rest of GitHub, which
                only index skills and can’t be browsed here.
              </p>
              <div className="sk-list">
                {sources.map((s) => (
                  <button key={sourceKey(s)} className="sk-row" onClick={() => openSource(s)}>
                    <span className="sk-rowicon" aria-hidden="true">{BOOK}</span>
                    <span className="sk-rowbody">
                      <div className="sk-rowname">{s.label}</div>
                      <div className="sk-rowslug">{s.repo}{s.path ? `/${s.path}` : ''}</div>
                      {s.blurb && <div className="sk-rowdesc">{s.blurb}</div>}
                    </span>
                    <span className="sk-chev" aria-hidden="true">{CHEV}</span>
                  </button>
                ))}
              </div>
            </>
          )}

          {detailDir && (
            detailEntry === 'failed' ? (
              <div className="sk-empty">
                <div className="sk-empty-mark" aria-hidden="true">⚠️</div>
                <div className="sk-empty-title">Couldn’t load this skill</div>
                <p className="sk-empty-text">SKILL.md for “{detailName}” couldn’t be fetched from GitHub.</p>
                {open && (
                  <a className="sk-btn ghost" href={githubSkillUrl(open.source, detailDir)} target="_blank" rel="noopener noreferrer">
                    Read on GitHub {EXTERNAL}
                  </a>
                )}
              </div>
            ) : detailLoaded ? (
              <div className="sk-md" onClick={onDetailClick} dangerouslySetInnerHTML={{ __html: detailHtml }} />
            ) : (
              <div className="sk-empty"><div className="sk-spinner" /></div>
            )
          )}

          {open && !detailDir && (
            <>
              {skillList !== null && skillList.length > 8 && (
                <div className="sk-searchwrap">
                  <div className="sk-search">
                    {SEARCH}
                    <input
                      className="sk-input" type="search" value={filter}
                      placeholder={`Filter ${skillList.length} skills…`}
                      onChange={(e) => setFilter(e.target.value)}
                      aria-label="Filter skills"
                    />
                  </div>
                </div>
              )}
              {skillList !== null && (
                <div className="sk-cat-count">
                  {skillList.length} {skillList.length === 1 ? 'skill' : 'skills'}
                  {truncated ? ' — large repo, GitHub truncated the list; some may be missing' : ''}
                </div>
              )}
              {scanBusy && (
                <div className="sk-empty"><div className="sk-spinner" /><div className="sk-empty-title">Scanning {open.source.repo}…</div></div>
              )}
              {shown && shown.length > 0 && (
                <div className="sk-cards">
                  {shown.map((s) => (
                    <CatalogCard
                      key={s.dir}
                      skill={s}
                      desc={descs[s.dir]}
                      installed={existingIds.has(s.name)}
                      busy={busyDir === s.dir}
                      onOpen={() => openSkillPage(s.dir)}
                      onLoad={() => loadDescription(open.source, s.dir)}
                      onInstall={() => install(open.source, s.dir)}
                    />
                  ))}
                </div>
              )}
              {shown && shown.length === 0 && !scanBusy && (
                <div className="sk-empty">
                  <div className="sk-empty-mark" aria-hidden="true">{SEARCH}</div>
                  <div className="sk-empty-title">{skillList.length ? 'No matches' : 'No skills here'}</div>
                  <p className="sk-empty-text">
                    {skillList.length ? `No skills match “${filter}”.` : 'No SKILL.md files found in this source.'}
                  </p>
                </div>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

export default function SkillsApp({ appId, token }) {
  const [skills, setSkills] = useState(null) // null = never loaded; [] or [..] = last-known-good
  const [systemPromptApps, setSystemPromptApps] = useState([])
  const [loadError, setLoadError] = useState(null) // user-facing copy for the latest failed load
  const [refreshing, setRefreshing] = useState(false)
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState(null) // id of open skill
  const [contents, setContents] = useState({}) // id -> { status, text } (lazy detail fetch)
  const [removeArmed, setRemoveArmed] = useState(false)
  const [removeBusy, setRemoveBusy] = useState(false)
  const [removeError, setRemoveError] = useState(null)
  const [catalogOpen, setCatalogOpen] = useState(false)
  const [catalogMounted, setCatalogMounted] = useState(false)
  const [online, setOnline] = useState(initialOnline)
  // The detail back-sentinel state machine (in domain.js so it is unit-testable
  // — the double-tap-during-pending-push race can't be exercised through the
  // React component alone). Created once; onShow/onClose close over the stable
  // setState + signal, getNavOpen resolves the runtime handle at call time.
  const detailNavRef = useRef(null)
  if (!detailNavRef.current) {
    detailNavRef.current = createDetailNav({
      label: 'skill-detail',
      getNavOpen: () => window.mobius?.nav?.open,
      onShow: (id) => { setSelected(id); window.mobius?.signal?.('item_opened', { type: 'skill', slug: id }) },
      onClose: () => setSelected(null),
    })
  }
  const detailNav = detailNavRef.current
  // Second sentinel for the catalog screen — one shell back target per pushed
  // screen; navigation INSIDE the catalog (sources ↔ source) is in-screen.
  const catalogNavRef = useRef(null)
  if (!catalogNavRef.current) {
    catalogNavRef.current = createDetailNav({
      label: 'skills-catalog',
      getNavOpen: () => window.mobius?.nav?.open,
      onShow: () => { setCatalogMounted(true); setCatalogOpen(true); window.mobius?.signal?.('item_opened', { type: 'catalog' }) },
      onClose: () => setCatalogOpen(false),
    })
  }
  const catalogNav = catalogNavRef.current
  const readySignalledRef = useRef(false) // gate app_ready to the first successful load
  const contentsRef = useRef(contents)
  contentsRef.current = contents
  const systemPromptAppsLoaderRef = useRef(null)
  if (!systemPromptAppsLoaderRef.current) {
    systemPromptAppsLoaderRef.current = createSystemPromptAppsLoader({
      fetchImpl: (...args) => fetch(...args),
      onApps: setSystemPromptApps,
    })
  }

  const authHeaders = useMemo(() => ({ Authorization: `Bearer ${token}` }), [token])

  // A failed refresh must NOT wipe the already-loaded list. load() keeps the
  // last-known-good `skills` on failure and only records `loadError`; the full
  // error empty state is reserved for the very first load (skills === null).
  async function load({ isRefresh = false } = {}) {
    // Fire-and-forget by construction: Refresh completion depends only on the
    // skills request. The loader commits [] on every apps failure and ignores
    // stale generations when refreshes overlap.
    systemPromptAppsLoaderRef.current.load(authHeaders)
    try {
      // One metadata call replaces the old per-file storage crawl — and unlike
      // a directory listing it includes directory-shaped skills, provenance,
      // and usage. Full markdown is fetched lazily when a detail opens.
      const res = await fetch('/api/skills', { headers: authHeaders })
      if (!res.ok) throw new Error(`list ${res.status}`)
      const data = await res.json()
      const rows = (Array.isArray(data?.skills) ? data.skills : [])
        .map((s) => {
          const id = String(s?.id ?? '')
          const name = typeof s?.name === 'string' && s.name ? s.name : id
          return {
            id,
            name,
            title: skillDisplayTitle(name),
            description: typeof s?.description === 'string' ? s.description : '',
            provenance: typeof s?.provenance === 'string' ? s.provenance : '',
            is_dir: !!s?.is_dir,
            uses: Number(s?.uses_30d) || 0,
          }
        })
        .filter((s) => s.id)
      rows.sort((a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase()))
      setSkills(rows)
      setLoadError(null)
      if (!readySignalledRef.current) {
        readySignalledRef.current = true
        window.mobius?.signal?.('app_ready', { item_count: rows.length })
      }
    } catch (err) {
      setLoadError(friendlyLoadError(err))
      window.mobius?.signal?.('error', { message: String(err?.message || err), source: isRefresh ? 'refresh' : 'load' })
      // Keep the last-known-good list intact; on the first load skills stays null.
    }
  }

  useEffect(() => {
    load()
    return () => systemPromptAppsLoaderRef.current.invalidate()
  }, []) // the skills API has no subscribe(); refresh is explicit

  async function refresh() {
    setRefreshing(true)
    setContents({}) // an explicit refresh also drops cached detail markdown
    await load({ isRefresh: true })
    setRefreshing(false)
  }

  // Track connectivity for the Offline pill (silent-sync: pill only when offline).
  useEffect(() => {
    if (typeof window.mobius?.onOnlineChange === 'function') {
      return window.mobius.onOnlineChange((next) => setOnline(!!next))
    }
    // Runtime fallback: standalone/older shells may expose only browser events.
    const on = () => setOnline(true)
    const off = () => setOnline(false)
    window.addEventListener('online', on)
    window.addEventListener('offline', off)
    if (typeof window.mobius?.online === 'boolean') setOnline(window.mobius.online)
    return () => { window.removeEventListener('online', on); window.removeEventListener('offline', off) }
  }, [])

  // Open (or cross-link-swap) a skill detail through the await-ready state
  // machine. All the sentinel lifecycle + race handling lives in detailNav.
  const openSkill = (id) => detailNav.open(id)
  const closeSkill = () => detailNav.close()
  const openCatalog = () => catalogNav.open('catalog')
  const closeCatalog = () => catalogNav.close()

  // If a refresh drops the currently-open skill, close the detail so we don't
  // leak the nav sentinel (a later device back would otherwise be consumed).
  useEffect(() => {
    if (selected && skills && !skills.some((s) => s.id === selected)) closeSkill()
  }, [selected, skills])

  function askAgent(draft) {
    window.parent.postMessage({ type: 'moebius:new-chat', draft }, window.location.origin)
  }

  function findSkills() {
    window.mobius?.signal?.('find_skills_requested', {})
    askAgent(FIND_DRAFT)
  }

  const current = selected && skills ? skills.find((s) => s.id === selected) : null

  // Lazy detail fetch: the list is metadata-only now, so the full markdown is
  // pulled from shared storage the first time a skill opens (then cached until
  // an explicit refresh).
  useEffect(() => {
    if (!current || contentsRef.current[current.id]) return
    const { id } = current
    const path = skillContentPath(current)
    setContents((c) => ({ ...c, [id]: { status: 'loading' } }))
    fetch(path, { headers: authHeaders })
      .then(async (r) => {
        if (!r.ok) throw new Error(`content ${r.status}`)
        const text = await r.text()
        setContents((c) => ({ ...c, [id]: { status: 'ready', text } }))
      })
      .catch((err) => {
        window.mobius?.signal?.('error', { message: String(err?.message || err), source: 'skill_load', status: 0 })
        setContents((c) => ({ ...c, [id]: { status: 'failed' } }))
      })
  }, [selected, skills])

  // Uninstall is a two-tap: first tap arms (danger ring + explainer), a second
  // within 4s executes. Modal confirms don't exist inside the sandboxed iframe.
  useEffect(() => { setRemoveArmed(false); setRemoveError(null) }, [selected])
  useEffect(() => {
    if (!removeArmed) return undefined
    const t = setTimeout(() => setRemoveArmed(false), 4000)
    return () => clearTimeout(t)
  }, [removeArmed])

  async function deleteSkill(id) {
    const res = await fetch(`/api/skills/${encodeURIComponent(id)}`, {
      method: 'DELETE', headers: authHeaders,
    })
    const data = await res.json().catch(() => ({}))
    if (!res.ok) throw new Error(data?.detail || `uninstall ${res.status}`)
    window.mobius?.signal?.('skill_uninstalled', { slug: id })
  }

  async function uninstallCurrent() {
    if (!current || removeBusy) return
    setRemoveBusy(true)
    setRemoveError(null)
    try {
      await deleteSkill(current.id)
      closeSkill()
      load({ isRefresh: true })
    } catch (err) {
      setRemoveError(String(err?.message || err))
      window.mobius?.signal?.('error', { message: String(err?.message || err), source: 'skill_uninstall' })
    } finally {
      setRemoveBusy(false)
      setRemoveArmed(false)
    }
  }

  // Keep the app mounted when a link is tapped inside a rendered skill.
  function onDetailClick(e) {
    const a = e.target.closest && e.target.closest('a')
    if (!a) return
    const link = classifyLink(a.getAttribute('href'))
    if (link.kind === 'anchor') return // in-page fragment — harmless, leave default
    if (link.kind === 'skill') {
      e.preventDefault()
      if (skills && skills.some((s) => s.id === link.slug)) {
        openSkill(link.slug)
      } else {
        window.mobius?.signal?.('error', { message: `unknown skill link ${link.slug}`, source: 'skill_link' })
      }
      return
    }
    if (link.kind === 'external') {
      e.preventDefault()
      window.open(link.url, '_blank', 'noopener,noreferrer')
      return
    }
    // Unsupported protocol or sub-path: block the navigation, keep the app up.
    e.preventDefault()
    window.mobius?.signal?.('error', { message: `blocked link (${link.reason})`, source: 'skill_link' })
  }

  const filtered = useMemo(() => {
    if (!skills) return []
    const q = query.trim().toLowerCase()
    if (!q) return skills
    return skills.filter((s) =>
      s.title.toLowerCase().includes(q) || s.id.toLowerCase().includes(q) || s.description.toLowerCase().includes(q))
  }, [skills, query])

  // Search analytics for Reflection: emit once per settled query (debounced so a
  // single search isn't counted once per keystroke). Payload is counts only —
  // never the raw term, which is free-text owner input. Depend on `query` alone
  // so a background refresh (new skills → new filtered) doesn't re-fire; filtered
  // is read at fire time and is already current for this query.
  useEffect(() => {
    const q = query.trim()
    if (!q) return
    const t = setTimeout(() => {
      window.mobius?.signal?.('search_performed', { query_length: q.length, result_count: filtered.length })
      if (filtered.length === 0) window.mobius?.signal?.('search_no_results', { query_length: q.length })
    }, 500)
    return () => clearTimeout(t)
  }, [query])

  const currentContent = current ? contents[current.id] : null
  const detailParsed = useMemo(() => {
    if (!current || currentContent?.status !== 'ready') return null
    return parseSkill(`${current.id}.md`, currentContent.text || '')
  }, [current, currentContent])
  const detailHtml = useMemo(() => {
    if (!detailParsed) return ''
    try {
      return DOMPurify.sanitize(marked.parse(detailParsed.content || ''))
    } catch (err) {
      window.mobius?.signal?.('error', { message: String(err?.message || err), source: 'markdown_render' })
      return ''
    }
  }, [detailParsed])

  const existingIds = useMemo(() => new Set((skills || []).map((s) => s.id)), [skills])

  const syncPill = !online
    ? <div className="sk-sync-pill" role="status">Offline</div>
    : (loadError && skills)
      ? <div className="sk-sync-pill is-error" role="status">Couldn’t refresh</div>
      : null

  // ---- Detail view ----
  if (current) {
    const removable = isUninstallable(current.provenance)
    return (
      <div className="sk-root">
        <style>{CSS}</style>
        {syncPill}
        <div className="sk-detail-head">
          <button className="sk-back" onClick={closeSkill} aria-label="Back to skills">{BACK}<span>Skills</span></button>
          <div className="sk-detail-title">{detailParsed?.title || current.title}</div>
          {removable && (
            <button
              className={`sk-iconbtn${removeArmed ? ' is-armed' : ''}`}
              disabled={removeBusy}
              onClick={() => (removeArmed ? uninstallCurrent() : setRemoveArmed(true))}
              aria-label={removeArmed ? 'Tap again to remove this skill' : 'Remove this skill'}
            >{TRASH}<span className="sk-tip" aria-hidden="true"><b>Delete</b> – removes the skill (asks once more before deleting)</span></button>
          )}
          <button className="sk-iconbtn" onClick={() => {
            window.mobius?.signal?.('edit_requested', { type: 'skill', slug: current.id })
            askAgent(`Help me edit the "${current.id}" skill. Here's what I want to change: `)
          }} aria-label="Edit skill with the agent">{PLUS}<span className="sk-tip" aria-hidden="true"><b>Edit</b> – opens a chat with the agent to change the skill</span></button>
        </div>
        {removeArmed && !removeError && (
          <div className="sk-alert" role="status">Tap the bin again to remove “{current.id}”. Its bytes are saved to git history first.</div>
        )}
        {removeError && <div className="sk-alert is-error" role="alert">{removeError}</div>}
        <div className="sk-scroll">
          <div className="sk-detailmeta">
            <ProvChips provenance={current.provenance} uses={current.uses} />
          </div>
          {currentContent?.status === 'failed' ? (
            <div className="sk-empty">
              <div className="sk-empty-mark" aria-hidden="true">⚠️</div>
              <div className="sk-empty-title">Couldn’t load this skill</div>
              <p className="sk-empty-text">The file for “{current.id}” couldn’t be read. Try refreshing, or ask the agent to check it.</p>
            </div>
          ) : currentContent?.status === 'ready' ? (
            <div className="sk-md" onClick={onDetailClick} dangerouslySetInnerHTML={{ __html: detailHtml }} />
          ) : (
            <div className="sk-empty"><div className="sk-spinner" /></div>
          )}
        </div>
      </div>
    )
  }

  // ---- List view (the catalog screen overlays it when open) ----
  const loading = skills === null && !loadError
  const initialError = skills === null && loadError
  return (
    <div className="sk-root">
      <style>{CSS}</style>
      {syncPill}
      <header className="sk-header">
        <div className="sk-brand">
          <span className="sk-mark">
            {appId ? (
              <img
                src={`/api/apps/${appId}/icon?size=64`}
                alt=""
                width={34}
                height={34}
                onError={(e) => {
                  e.currentTarget.style.display = 'none'
                  const f = e.currentTarget.nextElementSibling
                  if (f) f.style.display = 'flex'
                }}
              />
            ) : null}
            <span className="sk-mark-fallback" aria-hidden="true">·</span>
          </span>
          <div>
            <h1 className="sk-title">Skills</h1>
            <span className="sk-subtitle">{skills ? `${skills.length} agent ${skills.length === 1 ? 'skill' : 'skills'}` : 'Your agent’s abilities'}</span>
          </div>
        </div>
        <button className="sk-iconbtn" onClick={findSkills} aria-label="Ask the agent to find a new skill">
          {SPARKLE}<span className="sk-tip" aria-hidden="true"><b>Find</b> – use agent to find a skill you need</span>
        </button>
        <button className="sk-iconbtn" onClick={openCatalog} aria-label="Browse skill catalogs">
          {BOOK}<span className="sk-tip" aria-hidden="true"><b>Browse</b> – look at the public skill catalogs</span>
        </button>
        <button className={`sk-iconbtn${refreshing ? ' is-spinning' : ''}`} onClick={refresh} disabled={refreshing} aria-label="Refresh skills">
          {REFRESH}<span className="sk-tip" aria-hidden="true"><b>Refresh</b> – update the skills list</span>
        </button>
      </header>

      <div className="sk-scroll">
        <div className="sk-page">
        {skills !== null && skills.length > 0 && (
          <div className="sk-searchwrap">
            <div className="sk-search">
              {SEARCH}
              <input className="sk-input" type="search" value={query} placeholder="Search skills…"
                onChange={(e) => setQuery(e.target.value)} aria-label="Search skills" />
            </div>
          </div>
        )}

        {loading && (
          <div className="sk-empty"><div className="sk-spinner" /><div className="sk-empty-title">Loading skills…</div></div>
        )}

        {initialError && (
          <div className="sk-empty">
            <div className="sk-empty-mark" aria-hidden="true">⚠️</div>
            <div className="sk-empty-title">Couldn’t load skills</div>
            <p className="sk-empty-text">{loadError}</p>
            <button className="sk-retry" onClick={refresh}>Try again</button>
          </div>
        )}

        {skills !== null && skills.length === 0 && (
          <div className="sk-empty">
            <div className="sk-empty-mark" aria-hidden="true">{HAMMER}</div>
            <div className="sk-empty-title">No skills yet</div>
            <p className="sk-empty-text">Skills extend what your agent can do. Ask the agent to find or create one and it’ll appear here.</p>
            <button className="sk-retry" onClick={findSkills}>Find a skill</button>
            <button className="sk-retry" onClick={() => {
              window.mobius?.signal?.('item_created', { type: 'skill' })
              askAgent('Create a new skill for me. It should: ')
            }}>Ask the agent</button>
          </div>
        )}

        {skills !== null && skills.length > 0 && filtered.length === 0 && (
          <div className="sk-empty">
            <div className="sk-empty-mark" aria-hidden="true">{SEARCH}</div>
            <div className="sk-empty-title">No matches</div>
            <p className="sk-empty-text">No skills match “{query}”.</p>
          </div>
        )}

        {skills !== null && filtered.length > 0 && (
          <div className="sk-list">
            {filtered.map((s) => (
              <button key={s.id} className="sk-row" onClick={() => openSkill(s.id)} title={`Open “${s.title}”`}>
                <span className="sk-rowicon" aria-hidden="true">{HAMMER}</span>
                <span className="sk-rowbody">
                  <div className="sk-rowname">{s.title}</div>
                  <div className="sk-rowslug">{s.id}</div>
                  {s.description && <div className="sk-rowdesc">{s.description}</div>}
                  <ProvChips provenance={s.provenance} uses={s.uses} />
                </span>
                <span className="sk-chev" aria-hidden="true">{CHEV}</span>
              </button>
            ))}
          </div>
        )}

        {skills !== null && systemPromptApps.length > 0 && (
          <section className="sk-system-apps" aria-labelledby="system-prompt-apps-title">
            <h2 className="sk-section-title" id="system-prompt-apps-title">Apps that extend the agent</h2>
            <p className="sk-section-copy">These installed apps add always-on instructions to the agent for as long as they stay installed. Start a new chat after installing or uninstalling one to be sure the agent is working from the latest set.</p>
            <div className="sk-app-list">
              {systemPromptApps.map((app) => {
                const displayName = installedAppDisplayName(app)
                const description = typeof app.description === 'string' ? app.description.trim() : ''
                return (
                  <div className="sk-app-row" key={app.id}>
                    <span className="sk-app-icon" aria-hidden="true">
                      <img
                        src={`/api/apps/${app.id}/icon?size=64`}
                        alt=""
                        width={40}
                        height={40}
                        loading="lazy"
                        onError={(e) => {
                          e.currentTarget.style.display = 'none'
                          const fallback = e.currentTarget.nextElementSibling
                          if (fallback) fallback.style.display = 'flex'
                        }}
                      />
                      <span className="sk-app-icon-fallback">{displayName.charAt(0).toUpperCase()}</span>
                    </span>
                    <div className="sk-rowbody">
                      <div className="sk-rowname">{displayName}</div>
                      {description && <div className="sk-app-note">{description}</div>}
                    </div>
                  </div>
                )
              })}
            </div>
          </section>
        )}
        </div>
      </div>

      {catalogMounted && (
        <CatalogScreen
          visible={catalogOpen}
          authHeaders={authHeaders}
          existingIds={existingIds}
          onInstalled={() => load({ isRefresh: true })}
          onClose={closeCatalog}
        />
      )}
    </div>
  )
}
