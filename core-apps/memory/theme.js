export const CSS = `
/* mobius-ui:Root v1 — keep in sync; library candidate. Memory still owns
   most layout through S.* inline constants, so this block is the shared
   platform floor rather than a full rewrite. */
.mg-root {
  position: relative;
  display: flex;
  flex-direction: column;
  height: 100%;
  width: 100%;
  max-width: 100%;
  overflow: hidden;
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  -webkit-font-smoothing: antialiased;
  -webkit-tap-highlight-color: transparent;
}
/* /mobius-ui:Root */

/* mobius-ui:Focus v1 -- shared keyboard focus ring (WCAG 2.4.7); never bare outline:none */
:where(button,a,input,textarea,select,summary,[role="button"],[tabindex]:not([tabindex="-1"])):focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
/* /mobius-ui:Focus */

@keyframes mg-orbit-spin { to { transform: rotate(360deg); } }
.mg-orbit {
  position: relative; width: 46px; height: 46px;
  animation: mg-orbit-spin 2.4s linear infinite;
}
.mg-orbit span {
  position: absolute; width: 8px; height: 8px; border-radius: 50%;
  background: var(--accent); top: 50%; left: 50%; margin: -4px;
}
.mg-orbit span:nth-child(1) { transform: rotate(0deg) translateX(18px); opacity: 1; }
.mg-orbit span:nth-child(2) { transform: rotate(120deg) translateX(18px); opacity: 0.6; }
.mg-orbit span:nth-child(3) { transform: rotate(240deg) translateX(18px); opacity: 0.3; }

@keyframes mg-twinkle { 0%,100% { opacity: 0.35; } 50% { opacity: 1; } }
.mg-star { animation: mg-twinkle 2.8s ease-in-out infinite; }
.mg-star-hub { filter: drop-shadow(0 0 6px var(--accent)); }
@keyframes mg-pulse-ring {
  0% { transform: scale(0.8); opacity: 0.5; }
  70% { transform: scale(1.5); opacity: 0; }
  100% { opacity: 0; }
}
.mg-pulse { transform-origin: 66px 48px; animation: mg-pulse-ring 2.6s ease-out infinite; }

.mg-graph { cursor: grab; }
.mg-graph:active { cursor: grabbing; }

@media (hover: hover) {
  .mg-row:hover { background: var(--surface2); }
  .mg-th:hover { color: var(--text); }
  .mg-legend-row:hover { background: var(--surface2); }
  .mg-tgl:hover { color: var(--text); }
  .mg-tab:hover { color: var(--text); }
  .mg-close:hover { background: var(--border); color: var(--text); }
  .mg-discuss:hover { filter: brightness(1.06); }
}
/* Keyboard-focus ring for the now-focusable list rows + sort-header buttons,
   so the keyboard affordance these gained is actually visible. */
.mg-row:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.mg-th:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 4px; }
.mg-discuss:active { transform: translateY(1px); }

/* mobius-ui:Scrollskin v2 — keep in sync; hidden by default, content stays scrollable. */
.mg-scroll,
.mg-md pre {
  scrollbar-width: none;
  -ms-overflow-style: none;
}
.mg-scroll::-webkit-scrollbar,
.mg-md pre::-webkit-scrollbar {
  display: none;
  width: 0;
  height: 0;
}
/* /mobius-ui:Scrollskin */

@keyframes mg-skel-pulse { 0%,100% { opacity: 0.5; } 50% { opacity: 1; } }
@keyframes mg-pulse { 0%,100% { opacity: 0.4; } 50% { opacity: 1; } }
.mg-skel {
  height: 13px; border-radius: 5px;
  background: linear-gradient(90deg, var(--surface2), var(--border), var(--surface2));
  animation: mg-skel-pulse 1.4s ease-in-out infinite;
}

@keyframes mg-panel-in {
  from { transform: translateX(20px); opacity: 0; }
  to { transform: translateX(0); opacity: 1; }
}
@keyframes mg-scrim-in { from { opacity: 0; } to { opacity: 1; } }
.mg-panel { inset: 0 0 0 auto; width: min(980px, 96vw); animation: mg-panel-in 0.22s cubic-bezier(0.22,1,0.36,1); }
.mg-scrim { animation: mg-scrim-in 0.2s ease; }
.mg-local-graph { cursor: grab; background: var(--bg); }
.mg-local-graph:active { cursor: grabbing; }
.mg-md a[href^="#memory-node-"] {
  border: 1px solid var(--accent-dim, rgba(167,139,250,0.35));
  background: var(--accent-dim, rgba(167,139,250,0.12));
  border-radius: 6px;
  padding: 0 5px;
  font-weight: 600;
}
@media (max-width: 640px) {
  .mg-scrim { display: none; }
  .mg-panel {
    inset: 0; width: 100%; height: 100%; border-left: none;
    border-top: none; border-radius: 0; box-shadow: none;
    animation: mg-panel-in 0.18s cubic-bezier(0.22,1,0.36,1);
  }
  .mg-panel-head { padding: 11px 12px 8px !important; }
  .mg-panel .mg-close {
    width: 34px !important; height: 34px !important; border-radius: 10px !important;
  }
  .mg-panel .mg-tag-row {
    flex-wrap: nowrap !important; overflow-x: auto; padding: 0 12px 7px !important;
    scrollbar-width: none;
  }
  .mg-panel .mg-tag-row::-webkit-scrollbar { display: none; }
  .mg-md {
    padding: 10px 14px 18px !important;
    font-size: 13px !important;
    line-height: 1.54 !important;
  }
  .mg-md h1 { font-size: 17px !important; }
  .mg-md h2 { font-size: 15px !important; }
  .mg-md h3 { font-size: 13px !important; }
  .mg-md p { margin: 8px 0 !important; }
  .mg-md ul, .mg-md ol { margin: 8px 0 !important; }
  .mg-md code { font-size: 0.82em !important; }
  .mg-panel .mg-discuss { padding: 9px 12px !important; }
  .mg-scroll table th:nth-child(n+3),
  .mg-scroll table td:nth-child(n+3) {
    display: none;
  }
  .mg-scroll table th,
  .mg-scroll table td {
    padding-left: 10px !important;
    padding-right: 10px !important;
  }
}
/* mobius-ui:ReducedMotion v1 — keep in sync; library candidate. Diverge below the marker only. */
@media (prefers-reduced-motion: reduce) {
  .mg-orbit, .mg-star, .mg-pulse, .mg-skel, .mg-panel, .mg-scrim, .mg-star-hub { animation: none !important; }
}
/* /mobius-ui:ReducedMotion */

.mg-md h1, .mg-md h2, .mg-md h3 { margin: 16px 0 7px; line-height: 1.25; font-weight: 700; letter-spacing: 0; }
.mg-md h1 { font-size: 19px; } .mg-md h2 { font-size: 16px; } .mg-md h3 { font-size: 14px; }
.mg-md h1:first-child, .mg-md h2:first-child, .mg-md h3:first-child { margin-top: 0; }
.mg-md p { margin: 9px 0; }
.mg-md ul, .mg-md ol { margin: 9px 0; padding-left: 22px; }
.mg-md li { margin: 4px 0; }
.mg-md li::marker { color: var(--muted); }
.mg-md a { color: var(--accent); text-decoration: none; border-bottom: 1px solid var(--accent-dim, rgba(167,139,250,0.4)); }
.mg-md a:hover { border-bottom-color: var(--accent); }
.mg-md strong { color: var(--text); font-weight: 700; }
.mg-md code { background: var(--surface2); border-radius: 5px; padding: 1px 5px; font-family: var(--mono); font-size: 0.85em; border: 1px solid var(--border-light, var(--border)); }
.mg-md pre { background: var(--surface2); border: 1px solid var(--border); border-radius: 9px; padding: 13px; overflow-x: auto; margin: 11px 0; }
.mg-md pre code { background: none; padding: 0; border: none; }
.mg-md blockquote {
  margin: 11px 0; padding: 10px 13px;
  border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--border));
  border-radius: 8px;
  background: color-mix(in srgb, var(--accent) 9%, transparent);
  color: var(--muted);
}
.mg-md table { border-collapse: collapse; margin: 11px 0; font-size: 13px; width: 100%; }
.mg-md th, .mg-md td { border: 1px solid var(--border); padding: 6px 10px; text-align: left; }
.mg-md th { background: var(--surface2); font-weight: 600; }
.mg-md img { max-width: 100%; border-radius: 8px; }
.mg-md hr { border: none; border-top: 1px solid var(--border); margin: 16px 0; }
`;
