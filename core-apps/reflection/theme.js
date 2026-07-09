// Reflection stylesheet.

// ---------------------------------------------------------------------------
// Theme + motion. Structural colors are CSS variables so light + dark both
// work; the violet accent is the one committed hardcode. A handful of
// keyframes (drift, shimmer, rise) feed loading + entrance states so they feel
// alive rather than static.
//
// One module-level `const CSS` is rendered once at the app root as
// <style>{CSS}</style>; the app's class prefix is `rf-`. The shared chrome
// (Root / Header / Sheet / Empty / Card / Button / Input / Segmented /
// ChatEmbed / SyncPill) is fenced with `/* mobius-ui:<Block> v1 … */` markers
// so a future extraction is mechanical; app-specific blocks (aurora, moon
// tile, date tile, streak badge, brief↔chat split, settings) stay below as
// unfenced `rf-` rules and keep their exact current values. The violet
// accent (#7c6cf0 / #a78bfa) is the one committed hardcode the theme can't
// express. Structural colors are theme tokens.
// ---------------------------------------------------------------------------

const ACCENT = '#7c6cf0'        // reflection's own violet
const ACCENT_2 = '#a78bfa'      // lighter companion for gradients/glows
const ACCENT_DIM = 'rgba(124,108,240,0.13)'
const ACCENT_DIM_2 = 'rgba(167,139,250,0.10)'

export const CSS = `
@keyframes rf-drift {
  0%   { transform: translateY(0) rotate(0deg); opacity: .85; }
  50%  { transform: translateY(-6px) rotate(4deg); opacity: 1; }
  100% { transform: translateY(0) rotate(0deg); opacity: .85; }
}
@keyframes rf-rise {
  from { opacity: 0; transform: translateY(8px); }
  to   { opacity: 1; transform: translateY(0); }
}
@keyframes rf-spin {
  to { transform: rotate(360deg); }
}

/* mobius-ui:Focus v1 -- shared keyboard focus ring (WCAG 2.4.7); never bare outline:none */
:where(button,a,input,textarea,select,summary,[role="button"],[tabindex]:not([tabindex="-1"])):focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
/* /mobius-ui:Focus */

/* mobius-ui:Root v1 — keep in sync; library candidate. Diverge below the marker only. */
.rf-root {
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
.rf-scroll {
  flex: 1; min-height: 0;
  overflow-y: auto; overflow-x: hidden;
  padding: 16px 20px 40px;
  word-break: break-word; overflow-wrap: anywhere;
  position: relative; z-index: 1;
  overscroll-behavior: contain;
}
/* /mobius-ui:Root */

/* mobius-ui:Header v1 — keep in sync; library candidate. Diverge below the marker only. */
.rf-header {
  flex: 0 0 auto;
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  flex-wrap: wrap;
  padding: max(22px, env(safe-area-inset-top)) 20px 0;
  position: relative; z-index: 1;
}
.rf-brand { display: flex; align-items: center; gap: 11px; min-width: 0; }
.rf-brand-icon {
  flex: 0 0 auto; width: 34px; height: 34px; border-radius: 8px;
  object-fit: cover; display: block;
}
.rf-brand-fallback {
  flex: 0 0 auto; width: 34px; height: 34px; border-radius: 8px;
  align-items: center; justify-content: center;
  background: ${ACCENT}; color: var(--bg, #0c0c0c);
  font-weight: 700; line-height: 1;
}
.rf-header-right { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; flex: 0 0 auto; position: relative; z-index: 1; }
/* /mobius-ui:Header */

/* mobius-ui:Empty v1 — keep in sync; library candidate. Diverge below the marker only. */
.rf-empty {
  display: flex; flex-direction: column; align-items: center; text-align: center;
  max-width: 440px; margin: 0 auto; padding: 60px 24px 40px;
  color: var(--muted); font-size: 14px; line-height: 1.65;
}
.rf-empty-mark {
  width: 74px; height: 74px; margin: 0 auto 18px; border-radius: 22px;
  display: flex; align-items: center; justify-content: center;
  background: linear-gradient(160deg, ${ACCENT_DIM} 0%, ${ACCENT_DIM_2} 100%);
  border: 1px solid var(--border);
}
.rf-empty-mark-glyph { font-size: 34px; animation: rf-drift 6s ease-in-out infinite; }
.rf-empty-title { font-size: 17px; font-weight: 700; color: var(--text); letter-spacing: 0; margin-bottom: 8px; }
/* /mobius-ui:Empty */

/* mobius-ui:Card v1 — keep in sync; library candidate. Diverge below the marker only. */
.rf-card {
  display: flex; align-items: stretch; gap: 14px; width: 100%; min-height: 44px;
  padding: 15px 16px; text-align: left;
  background: var(--surface); color: var(--text); font-family: var(--font);
  border: 1px solid var(--border); border-radius: 16px;
  position: relative; overflow: hidden; cursor: pointer;
  transition: border-color .16s ease, transform .12s ease, box-shadow .16s ease, background .16s ease;
  touch-action: manipulation; user-select: none;
}
button.rf-card { cursor: pointer; }
@media (hover:hover) { .rf-card:hover { border-color: ${ACCENT}; background: color-mix(in srgb, var(--surface) 92%, ${ACCENT} 8%); } }
.rf-card:active { transform: scale(.992); background: var(--surface-active, var(--surface)); }
.rf-card:focus-visible { outline: 2px solid ${ACCENT}; outline-offset: 2px; }
.rf-card.is-latest {
  border-color: ${ACCENT};
  background: color-mix(in srgb, var(--surface) 88%, ${ACCENT} 12%);
}
/* /mobius-ui:Card */

/* mobius-ui:Button v1 — keep in sync; library candidate. Diverge below the marker only. */
.rf-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 5px;
  min-height: 44px; padding: 10px 16px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--surface); color: var(--text);
  font-family: var(--font); font-size: 13px; font-weight: 650; cursor: pointer; white-space: nowrap;
  transition: background .14s ease, border-color .14s ease, transform .1s ease, color .14s ease;
  touch-action: manipulation; user-select: none;
}
.rf-btn:active { transform: scale(.97); opacity: 0.85; }
.rf-btn:focus-visible { outline: 2px solid ${ACCENT}; outline-offset: 2px; }
.rf-btn:disabled { opacity: 0.5; cursor: default; transform: none; }
/* /mobius-ui:Button */

/* mobius-ui:Segmented v1 — keep in sync; library candidate. Diverge below the marker only. */
.rf-seg {
  display: flex; gap: 2px; padding: 3px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 10px;
}
.rf-seg-btn {
  min-height: 44px; padding: 6px 15px; border: none; border-radius: 7px;
  background: transparent; color: var(--muted); font-family: var(--font);
  font-size: 13px; font-weight: 650; cursor: pointer; transition: background .15s, color .15s;
  touch-action: manipulation; user-select: none;
}
@media (hover:hover) { .rf-seg-btn:hover { color: var(--text); } }
.rf-seg-btn.is-active { background: ${ACCENT}; color: var(--accent-fg); }
/* /mobius-ui:Segmented */

/* mobius-ui:ChatEmbed v1 — keep in sync; library candidate. Diverge below the marker only. */
.rf-chat-embed {
  flex: 1 1 auto;
  min-height: 0;   /* the flexbox overflow fix — lets the iframe scroll internally */
  width: 100%;
  overflow: hidden;
  background: var(--bg);
}
.rf-chat-embed iframe { display: block; width: 100%; height: 100%; border: 0; }
/* /mobius-ui:ChatEmbed */

/* mobius-ui:Spinner v1 — keep in sync; library candidate. */
.rf-spinner {
  width: 26px; height: 26px; border-radius: 50%;
  border: 2.5px solid ${ACCENT_DIM}; border-top-color: ${ACCENT};
  animation: rf-spin 0.8s linear infinite;
}
.rf-spinner-sm { width: 16px; height: 16px; border-width: 2px; }
@media (prefers-reduced-motion: reduce) { .rf-spinner { animation: none; } }
/* /mobius-ui:Spinner */

/* mobius-ui:Scrollskin v2 — keep in sync; hidden by default, content stays scrollable. */
.rf-scroll,
.rf-split-body {
  scrollbar-width: none;
  -ms-overflow-style: none;
}
.rf-scroll::-webkit-scrollbar,
.rf-split-body::-webkit-scrollbar {
  display: none;
  width: 0;
  height: 0;
}
/* /mobius-ui:Scrollskin */

/* ---- App-specific (reflection) — keep exact current values ---- */

/* The detail view reuses the shared empty shape for its missing/error notes,
   but with a tighter top inset than the list's first-run empty. */
.rf-empty.is-compact { padding-top: 56px; }

/* Entrance + press affordances. A handful of reflection surfaces ride a small
   rise-in on mount; pressable controls get the same active/focus feel as the
   shared card without being one. */
.rf-rise { animation: rf-rise .32s cubic-bezier(.22,.61,.36,1) both; }
.rf-pressable { transition: background .14s ease, border-color .14s ease, transform .1s ease, color .14s ease; touch-action: manipulation; user-select: none; }
.rf-pressable:active { transform: scale(.97); opacity: 0.85; }
.rf-pressable:focus-visible { outline: 2px solid ${ACCENT}; outline-offset: 2px; }

/* A faint aurora wash behind the header — pure decoration, pointer-none, so
   the top of the app reads as a sky rather than a flat bar. */
.rf-aurora {
  position: absolute; top: 0; left: 0; right: 0; height: 220px;
  background: radial-gradient(120% 90% at 18% -10%, ${ACCENT_DIM} 0%, transparent 55%), radial-gradient(110% 80% at 92% -20%, ${ACCENT_DIM_2} 0%, transparent 60%);
  pointer-events: none; z-index: 0;
}
.rf-divider { height: 1px; background: var(--border); margin: 16px 20px 0; position: relative; z-index: 1; }

/* Streak badge — header pill + standalone streak bar share the base; the bar
   variant bumps padding + font. */
.rf-streak-badge {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 5px 11px; border-radius: 999px;
  background: ${ACCENT_DIM}; color: ${ACCENT};
  border: 1px solid transparent;
  font-size: 12.5px; font-weight: 650; line-height: 1.2; white-space: nowrap;
}
.rf-streak-badge.is-quiet {
  background: var(--surface); color: var(--muted); border-color: var(--border);
}
.rf-streak-bar { max-width: 660px; margin: 0 auto 16px; display: flex; }
.rf-streak-bar .rf-streak-badge { padding: 7px 13px; font-size: 13px; }
.rf-streak-flame { animation: rf-drift 4s ease-in-out infinite; }
.rf-streak-num { font-weight: 750; }
.rf-streak-unit { font-weight: 550; }
.rf-streak-dots { margin-left: 4px; letter-spacing: 0; opacity: 0.55; font-size: 9px; }

/* Reports list + dated card */
.rf-list { display: flex; flex-direction: column; gap: 11px; max-width: 660px; margin: 0 auto; }
.rf-date-tile {
  width: 46px; flex-shrink: 0; border-radius: 12px;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  gap: 0; align-self: center;
  background: ${ACCENT_DIM}; color: ${ACCENT};
  padding: 8px 0; line-height: 1;
}
.rf-date-tile.is-latest {
  background: linear-gradient(160deg, ${ACCENT} 0%, ${ACCENT_2} 100%); color: var(--accent-fg);
}
.rf-date-tile-day { font-size: 10px; font-weight: 700; letter-spacing: 0; opacity: 0.92; }
.rf-date-tile-num { font-size: 19px; font-weight: 750; letter-spacing: 0; }
.rf-card-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; justify-content: center; }
.rf-card-label-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.rf-card-label { font-size: 16px; font-weight: 700; letter-spacing: 0; line-height: 1.2; }
.rf-card-sub { font-size: 12px; color: var(--muted); font-weight: 500; }
.rf-card-tldr {
  font-size: 13px; color: var(--muted); line-height: 1.5; margin-top: 5px;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
}
.rf-card-chevron { align-self: center; font-size: 20px; color: var(--muted); flex-shrink: 0; line-height: 1; opacity: 0.7; }
.rf-latest-pill {
  font-size: 11px; font-weight: 750; letter-spacing: 0;
  color: var(--accent-fg);
  background: ${ACCENT}; padding: 2px 8px; border-radius: 999px;
}

/* Loading / error / offline states */
.rf-loading-wrap { text-align: center; padding: 64px 24px; color: var(--muted); font-size: 13px; }
.rf-loading-wrap .rf-spinner { margin: 0 auto 14px; }
.rf-error-box {
  max-width: 660px; margin: 0 auto; padding: 16px; border-radius: 14px;
  border: 1px solid var(--border); background: var(--surface);
  color: var(--text); font-size: 13px; line-height: 1.55;
  display: flex; flex-direction: column; gap: 10px;
}
.rf-retry-btn {
  align-self: flex-start; padding: 7px 14px; border-radius: 9px;
  border: 1px solid ${ACCENT}; background: transparent; color: ${ACCENT};
  font-size: 12.5px; font-weight: 650; cursor: pointer; font-family: var(--font);
  touch-action: manipulation; user-select: none;
}
.rf-offline-banner {
  max-width: 660px; margin: 0 auto 14px; padding: 10px 14px;
  border-radius: 12px; background: ${ACCENT_DIM}; border: 1px solid var(--border);
  color: var(--text); font-size: 12.5px; line-height: 1.45;
  display: flex; align-items: center; gap: 8px;
}

/* Report detail — the brief read, with an app-scoped chat one tap away. A flex
   column (the bar, then the detail body); position:absolute + inset:0 gives the
   body a definite height so the chat panel's %-height resolves. */
.rf-detail {
  position: absolute; inset: 0; display: flex; flex-direction: column;
  background: var(--bg); z-index: 5;
}
/* The detail body. A flex column: the scrolling read on top, then (when chat is
   open) a draggable divider + the chat panel. min-height:0 lets the read
   shrink so the chat panel's %-height has room. Mirrors app-latex's .body. */
.rf-detail-body {
  flex: 1; min-height: 0;
  display: flex; flex-direction: column;
  overflow: hidden;
}
.rf-detail-bar {
  display: flex; align-items: center; gap: 12px;
  padding: max(11px, env(safe-area-inset-top)) 14px 11px;
  border-bottom: 1px solid var(--border);
  flex-shrink: 0; background: var(--surface);
}
.rf-back-btn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 7px 13px 7px 9px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--bg);
  color: var(--text); font-size: 13px; font-weight: 650;
  cursor: pointer; font-family: var(--font); flex-shrink: 0;
  touch-action: manipulation; user-select: none;
}
.rf-back-glyph { font-size: 16px; }
.rf-detail-title { display: flex; flex-direction: column; min-width: 0; line-height: 1.25; flex: 1; }
.rf-detail-title-main { font-size: 15px; font-weight: 700; letter-spacing: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.rf-detail-title-sub { font-size: 12px; color: var(--muted); font-weight: 500; }
.rf-split-body {
  flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden;
  display: flex; flex-direction: column;
  overscroll-behavior: contain;
}
.rf-brief-panel {
  flex-shrink: 0; display: flex; flex-direction: column;
  border-bottom: 1px solid var(--border);
}
.rf-brief-iframe { width: 100%; border: none; background: var(--bg); display: block; }
.rf-brief-loading {
  min-height: 320px; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 12px;
  color: var(--muted); font-size: 13px;
}
.rf-chat-resolving {
  padding: 20px 16px 28px; display: flex; align-items: center; gap: 10px;
  color: var(--muted); font-size: 12.5px;
}
.rf-no-chat-note {
  margin: 14px 16px 22px; padding: 14px 16px; border-radius: 13px;
  background: var(--surface); border: 1px dashed var(--border);
  color: var(--muted); font-size: 12.5px; line-height: 1.55;
  display: flex; align-items: flex-start; gap: 10px;
}
.rf-no-chat-glyph { font-size: 15px; line-height: 1.2; }

/* A one-line prompt at the top of the chat, nudging the owner to leave feedback
   on the day's brief (that's what this app-scoped chat is FOR). */
.rf-chat-hint {
  flex: 0 0 auto; padding: 9px 14px;
  font-size: 12px; line-height: 1.45; color: var(--muted);
  background: var(--surface); border-bottom: 1px solid var(--border);
}

/* The chat icon in the detail bar — sits to the right of the report title.
   Subdued when CLOSED (reads as an affordance, not an active state); accent-
   tinted only when OPEN, matching app-latex / app-webstudio. */
.rf-chat-toggle {
  display: inline-flex; align-items: center; justify-content: center;
  min-width: 44px; min-height: 44px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--bg);
  color: var(--text); cursor: pointer; flex-shrink: 0;
}
.rf-chat-toggle[aria-pressed="true"] {
  background: color-mix(in srgb, ${ACCENT} 18%, var(--surface));
  border-color: color-mix(in srgb, ${ACCENT} 40%, var(--border));
  color: ${ACCENT};
}

/* mobius-ui:ChatSplit v1 — the bottom half of the 50/50 chat split. Mirrors
   app-latex / app-webstudio so the chat reads the same across apps; keep in
   sync. The embedded shell chat runs in an iframe (window.mobius.chat). The
   panel takes the --chat-ratio share of the detail-body height, floored at
   --chat-pane-min (composer pill + divider) so the embed's input pill is never
   clipped, and capped at the same floor from the other end so the read never
   fully eats the chat. The drag/keyboard ratio math honors these bounds; the
   CSS floor also covers the persisted/default ratio on a short viewport before
   any drag. It's a flex column; .rf-chat-embed fills it (flex:1 + min-height:0)
   and the iframe fills the embed, pinning the composer to the panel's bottom. */
.rf-chat-panel {
  flex: 0 0 auto;
  height: calc(var(--chat-ratio, 0.5) * 100%);
  min-height: min(var(--chat-pane-min, 74px), 100%);
  max-height: calc(100% - var(--chat-pane-min, 74px));
  display: flex; flex-direction: column;
  background: var(--surface);
  overflow: hidden; overscroll-behavior: contain;
  /* Bottom-pinned: lift the embedded composer above the iPhone home-indicator
     / Android gesture bar on a full-screen PWA. */
  padding-bottom: env(safe-area-inset-bottom);
}
/* The draggable divider between read and chat: a slim 10px visual bar; the
   ::before overlay extends the pointer hit area to ~26px without adding visual
   weight; z-index keeps that overlay above the adjacent panes. */
.rf-chat-divider {
  flex: 0 0 10px;
  height: 10px;
  box-sizing: border-box;
  position: relative;
  z-index: 5;
  display: flex; align-items: center; justify-content: center;
  cursor: ns-resize;
  background: var(--surface);
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
  touch-action: none; user-select: none;
}
.rf-chat-divider::before {
  content: ''; position: absolute; left: 0; right: 0; top: -8px; bottom: -8px;
}
.rf-chat-divider:hover,
.rf-chat-divider:focus-visible {
  background: color-mix(in srgb, ${ACCENT} 12%, var(--surface));
}
.rf-chat-divider:focus-visible { outline-offset: -2px; }
.rf-chat-divider-bar {
  width: 44px; height: 4px; border-radius: 999px;
  background: color-mix(in srgb, var(--muted) 65%, transparent);
  pointer-events: none;
}
/* /mobius-ui:ChatSplit */

/* Last-night status row */
.rf-status-row {
  max-width: 660px; margin: 0 auto 14px; display: flex; align-items: center;
  gap: 10px; padding: 10px 14px; border-radius: 13px;
  border: 1px solid var(--border); background: var(--surface);
  font-size: 12.5px; line-height: 1.45; flex-wrap: wrap;
}
.rf-status-dot {
  width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
}
.rf-status-dot.ok   { background: var(--green, #3fb950); box-shadow: 0 0 0 3px rgba(63,185,80,.15); }
.rf-status-dot.fail { background: var(--danger, #f85149); box-shadow: 0 0 0 3px rgba(248,81,73,.15); }
.rf-status-dot.skip { background: var(--muted); }
.rf-status-dot.none { background: var(--border); }
.rf-status-label { flex: 1; color: var(--text); font-weight: 600; }
.rf-status-hint  { color: var(--muted); font-size: 12px; }
.rf-status-investigate {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 5px 12px; border-radius: 9px; border: 1px solid var(--danger, #f85149);
  background: transparent; color: var(--danger, #f85149);
  font-size: 12px; font-weight: 650; cursor: pointer; font-family: var(--font);
  touch-action: manipulation; user-select: none;
}

/* Settings */
.rf-settings-wrap { max-width: 580px; margin: 0 auto; display: flex; flex-direction: column; gap: 22px; }
.rf-settings-card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px; padding: 18px; display: flex; flex-direction: column; gap: 10px;
}
.rf-section-head { display: flex; align-items: center; gap: 10px; }
.rf-section-icon {
  width: 30px; height: 30px; border-radius: 9px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  background: ${ACCENT_DIM}; font-size: 15px;
}
.rf-section-label { font-size: 14.5px; font-weight: 700; letter-spacing: 0; margin: 0; }
.rf-note { font-size: 12.5px; color: var(--muted); margin: 0; line-height: 1.55; }
.rf-note-strong { color: var(--text); font-weight: 650; }
.rf-time-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-top: 2px; }
.rf-time-input {
  padding: 9px 12px; font-size: 16px; font-family: var(--font); font-weight: 600;
  background: var(--bg); color: var(--text);
  border: 1px solid var(--border); border-radius: 10px;
  width: 132px;
}
.rf-time-input:focus:not(:focus-visible) { outline: none; }
.rf-custom-cron-note {
  font-size: 12px; color: var(--muted); line-height: 1.5;
  padding: 10px 12px; border-radius: 11px;
  background: var(--bg); border: 1px solid var(--border); margin-top: 2px;
}
.rf-custom-cron-note .rf-time-row { margin-top: 10px; }
.rf-select {
  width: 100%; min-height: 44px; padding: 9px 12px;
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg); color: var(--text); font-size: 16px;
  font-family: var(--font); font-weight: 650;
  touch-action: manipulation; user-select: none;
}
.rf-select:focus:not(:focus-visible) { outline: none; }
.rf-agent-stack { display: grid; gap: 12px; }
.rf-agent-field { display: grid; gap: 7px; }
.rf-meta {
  font-size: 12px; color: var(--muted); line-height: 1.5;
  font-family: var(--mono, var(--font));
  padding: 10px 12px; border-radius: 11px;
  background: var(--bg); border: 1px solid var(--border);
}
.rf-model-label {
  font-size: 12px; color: var(--muted); font-weight: 750;
  letter-spacing: 0; margin-top: 4px;
}
.rf-textarea {
  width: 100%; min-height: 64px; padding: 9px 12px;
  border: 1px solid var(--border); border-radius: 10px;
  background: var(--bg); color: var(--text); font-size: 14px;
  font-family: var(--font); resize: vertical; line-height: 1.5;
  box-sizing: border-box;
}
.rf-textarea:focus:not(:focus-visible) { outline: none; }
.rf-verbosity-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 4px; }
.rf-verb-btn {
  flex: 1; min-height: 44px; padding: 8px 12px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--bg); color: var(--muted);
  font-size: 13px; font-weight: 650; cursor: pointer; font-family: var(--font);
  transition: background .14s, border-color .14s, color .14s;
  touch-action: manipulation; user-select: none; text-align: center;
}
.rf-verb-btn.is-active { border-color: ${ACCENT}; color: ${ACCENT}; background: ${ACCENT_DIM}; }
.rf-verb-hint { font-size: 11.5px; color: var(--muted); margin-top: 4px; line-height: 1.45; }
.rf-save-row { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-top: 2px; }
.rf-save-btn {
  padding: 10px 22px; border-radius: 12px; border: none;
  background: ${ACCENT}; color: var(--accent-fg);
  font-size: 13.5px; font-weight: 700; cursor: pointer;
  font-family: var(--font); transition: background 0.15s, opacity .15s;
  box-shadow: 0 4px 8px -4px ${ACCENT};
  touch-action: manipulation; user-select: none;
}
.rf-save-btn:disabled {
  background: var(--surface); color: var(--muted); cursor: default; box-shadow: none;
}
.rf-toast { font-size: 12.5px; color: var(--green, #3fb950); font-weight: 650; }
.rf-error-toast { font-size: 12.5px; color: var(--danger, #f85149); font-weight: 650; }
/* Dead-letter banner: a previously-"Saved" offline write the server later
   refused on drain. Lives at the app root (the originating form is usually
   unmounted by drain time) and reads as an honest retraction, not a toast. */
.rf-deadletter {
  display: flex; align-items: flex-start; gap: 10px;
  margin: 14px 20px 0; padding: 11px 13px;
  border-radius: 12px;
  font-size: 12.5px; line-height: 1.5; color: var(--danger, #f85149);
  background: var(--surface); border: 1px solid var(--danger, #f85149);
}
.rf-deadletter__x {
  flex: none; margin-left: auto; border: none; background: none;
  color: var(--muted); font-size: 16px; line-height: 1;
  padding: 0 2px; cursor: pointer;
  touch-action: manipulation; user-select: none;
}
.rf-schedule-hint {
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
.rf-rq {
  margin: 18px 16px 22px;
  padding: 16px 16px 18px;
  border-radius: 14px;
  border: 1px solid ${ACCENT};
  background: ${ACCENT_DIM};
}
.rf-rq__title { font-size: 15px; font-weight: 750; color: var(--text); margin: 0 0 4px; letter-spacing: 0; }
.rf-rq__note { font-size: 12px; color: var(--muted); margin: 0 0 14px; line-height: 1.5; }
.rf-rq__q + .rf-rq__q {
  margin-top: 16px; padding-top: 16px; border-top: 1px solid var(--border);
}
.rf-rq__header {
  font-size: 11px; font-weight: 600;
  letter-spacing: 0; color: ${ACCENT}; margin-bottom: 4px;
}
.rf-rq__text { font-size: 14px; margin-bottom: 6px; color: var(--text); line-height: 1.45; }
.rf-rq__hint { font-size: 11px; color: var(--muted); margin-bottom: 8px; }
.rf-rq__opts { display: flex; flex-wrap: wrap; gap: 6px; }
.rf-rq__opt {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 8px 13px; min-height: 38px;
  border-radius: 9px; border: 1px solid var(--border);
  background: var(--surface); color: var(--text);
  font-size: 13px; cursor: pointer; box-sizing: border-box;
  font-family: var(--font); touch-action: manipulation; user-select: none;
}
@media (hover: hover) {
  .rf-rq__opt:not(.rf-rq__opt--on):hover { border-color: ${ACCENT}; }
}
.rf-rq__opt--on { background: ${ACCENT}; color: var(--accent-fg); border-color: ${ACCENT}; }
.rf-rq__opt--dim { opacity: 0.4; border-color: transparent; }
.rf-rq__opt:disabled { cursor: default; }
.rf-rq__submit {
  display: block; width: 100%; margin-top: 14px; min-height: 44px;
  padding: 11px; border-radius: 11px; border: none;
  background: ${ACCENT}; color: var(--accent-fg);
  font-size: 14px; font-weight: 700; cursor: pointer;
  font-family: var(--font); touch-action: manipulation; user-select: none;
  box-shadow: 0 4px 8px -4px ${ACCENT};
}
.rf-rq__submit:disabled { opacity: 0.4; cursor: default; box-shadow: none; }
.rf-rq--answered .rf-rq__done {
  margin-top: 14px; font-size: 12.5px; color: var(--muted); line-height: 1.5;
}
.rf-rq__error {
  margin-top: 10px; font-size: 12.5px; color: var(--danger, #f85149); line-height: 1.5;
}

@media (prefers-reduced-motion: reduce) {
  .rf-rise, .rf-empty-mark-glyph, .rf-streak-flame { animation: none !important; }
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
