export const CSS = `
/* mobius-ui:AppShell - app-owned; pinned header with an independently scrolling sampler surface. */
.bm-root {
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
.bm-scroll {
  flex: 1;
  min-height: 0;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 14px 16px 18px;
  word-break: break-word;
  overflow-wrap: anywhere;
}
.bm-scroll > * { flex-shrink: 0; }
/* /mobius-ui:AppShell */

/* mobius-ui:Header - app-owned. */
.bm-header {
  flex: 0 0 auto;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  min-height: 58px;
  padding: max(12px, env(safe-area-inset-top)) 16px 12px;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
}
.bm-brand {
  display: flex;
  align-items: center;
  gap: 11px;
  min-width: 0;
}
.bm-brand-icon {
  flex: 0 0 auto;
  width: 36px;
  height: 36px;
  display: block;
  object-fit: contain;
  border-radius: 9px;
  filter: drop-shadow(0 2px 3px rgba(0,0,0,0.26));
}
.bm-mark {
  flex: 0 0 auto;
  width: 36px;
  height: 36px;
  border-radius: 10px;
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 4px;
  padding: 6px;
  background: color-mix(in srgb, var(--accent) 16%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent) 36%, var(--border));
}
.bm-mark span {
  border-radius: 4px;
  background: var(--accent);
  opacity: 0.9;
}
.bm-mark span:nth-child(2) { opacity: 0.62; }
.bm-mark span:nth-child(3) { opacity: 0.42; }
.bm-mark span:nth-child(4) { opacity: 0.76; }
.bm-brand-text {
  min-width: 0;
  line-height: 1.15;
}
.bm-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0;
  text-wrap: balance;
}
.bm-subtitle {
  display: block;
  margin-top: 3px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  font-variant-numeric: tabular-nums;
}
.bm-header-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: 0 0 auto;
}
/* /mobius-ui:Header */

.bm-main {
  min-height: 100%;
  display: grid;
  grid-template-columns: minmax(300px, 1fr) minmax(280px, 360px);
  gap: 12px;
  align-items: stretch;
}
.bm-bank {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.bm-bank-group {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.bm-section-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 30px;
}
.bm-section-title {
  margin: 0;
  font-size: 14px;
  font-weight: 700;
  letter-spacing: 0;
}
.bm-section-meta {
  color: var(--muted);
  font-size: 12px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.bm-pad-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 8px;
}
.bm-pad {
  position: relative;
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  aspect-ratio: 1;
  min-height: 70px;
  padding: 10px;
  border-radius: 12px;
  border: 1px solid color-mix(in srgb, var(--pad-color) 24%, var(--border));
  background:
    radial-gradient(circle at 28% 18%, color-mix(in srgb, var(--pad-color) 22%, transparent), transparent 38%),
    linear-gradient(145deg, color-mix(in srgb, var(--pad-color) 9%, var(--surface)), var(--surface));
  color: var(--text);
  font-family: var(--font);
  cursor: pointer;
  text-align: left;
  touch-action: manipulation;
  user-select: none;
  transition: transform 0.1s ease, border-color 0.15s ease, background 0.15s ease, filter 0.15s ease;
}
.bm-pad:hover {
  border-color: color-mix(in srgb, var(--pad-color) 58%, var(--border));
}
.bm-pad:active,
.bm-pad.is-active {
  transform: scale(0.972);
  filter: brightness(1.06);
}
.bm-pad.is-selected {
  border-color: var(--pad-color);
  background:
    radial-gradient(circle at 30% 18%, color-mix(in srgb, var(--pad-color) 30%, transparent), transparent 42%),
    linear-gradient(145deg, color-mix(in srgb, var(--pad-color) 15%, var(--surface)), var(--surface));
}
.bm-pad.is-empty {
  border-style: dashed;
  background: var(--surface);
  color: var(--muted);
}
.bm-pad.is-recording {
  border-color: var(--danger);
  background: color-mix(in srgb, var(--danger) 12%, var(--surface));
}
.bm-pad-index {
  color: color-mix(in srgb, var(--pad-color) 78%, var(--muted));
  font-size: 11px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}
.bm-pad-name {
  display: block;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text);
  font-size: 14px;
  font-weight: 700;
  line-height: 1.16;
}
.bm-pad-kind {
  color: var(--muted);
  font-size: 11px;
  font-weight: 600;
}
.bm-pad.is-empty .bm-pad-name,
.bm-pad.is-empty .bm-pad-index {
  color: var(--muted);
}

.bm-panel {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  overflow: hidden;
}
.bm-detail {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 14px;
}
.bm-detail-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.bm-detail-kicker {
  display: block;
  margin-bottom: 2px;
  color: var(--muted);
  font-size: 12px;
  font-weight: 650;
}
.bm-detail-title {
  margin: 0;
  color: var(--text);
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0;
  line-height: 1.2;
}
.bm-pill {
  display: inline-flex;
  align-items: center;
  min-height: 26px;
  padding: 4px 9px;
  border: 1px solid var(--border);
  border-radius: 999px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}
.bm-pill.is-recording {
  border-color: color-mix(in srgb, var(--danger) 55%, var(--border));
  color: var(--danger);
}
.bm-wave-wrap {
  min-height: 112px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg);
  overflow: hidden;
}
.bm-wave {
  display: block;
  width: 100%;
  height: 112px;
}
.bm-empty {
  min-height: 184px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 8px;
  text-align: center;
  color: var(--muted);
  padding: 22px 14px;
}
.bm-wave-wrap .bm-empty {
  min-height: 0;
  padding: 14px;
}
.bm-empty-mark {
  width: 54px;
  height: 54px;
  border-radius: 16px;
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 5px;
  padding: 10px;
  background: color-mix(in srgb, var(--accent) 12%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent) 26%, var(--border));
}
.bm-empty-mark span {
  border-radius: 5px;
  background: color-mix(in srgb, var(--accent) 76%, var(--surface));
}
.bm-empty-title {
  color: var(--text);
  font-size: 15px;
  font-weight: 700;
}
.bm-empty-text {
  max-width: 28ch;
  margin: 0;
  font-size: 13px;
  line-height: 1.45;
}
.bm-controls {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

/* mobius-ui:Button - app-owned. */
.bm-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 44px;
  padding: 10px 14px;
  border-radius: 10px;
  border: 1px solid var(--border);
  background: var(--surface);
  color: var(--text);
  font-family: var(--font);
  font-size: 14px;
  font-weight: 650;
  cursor: pointer;
  white-space: nowrap;
  transition: background 0.14s ease, border-color 0.14s ease, transform 0.1s ease, filter 0.14s ease;
}
.bm-btn:hover {
  border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
}
.bm-btn:active {
  transform: scale(0.97);
}
.bm-btn:disabled {
  cursor: default;
  opacity: 0.5;
  transform: none;
}
.bm-btn-primary {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--accent-fg);
}
.bm-btn-primary:hover { filter: brightness(1.06); }
.bm-btn-danger {
  background: var(--danger);
  border-color: var(--danger);
  color: var(--accent-fg);
}
.bm-btn-secondary {
  background: var(--surface2, var(--surface));
}
/* /mobius-ui:Button */

/* mobius-ui:Input - app-owned. */
.bm-input {
  display: block;
  width: 100%;
  min-height: 44px;
  padding: 10px 12px;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: var(--bg);
  color: var(--text);
  font-family: var(--font);
  font-size: 16px;
  outline: none;
}
.bm-input:focus {
  border-color: var(--accent);
  box-shadow: 0 0 0 1px var(--accent);
}
/* /mobius-ui:Input */

.bm-slider-row {
  display: grid;
  grid-template-columns: 64px minmax(0, 1fr) 40px;
  align-items: center;
  gap: 8px;
  min-height: 44px;
}
.bm-slider-label,
.bm-slider-value {
  color: var(--muted);
  font-size: 12px;
  font-weight: 650;
}
.bm-slider-value {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.bm-slider {
  width: 100%;
  min-width: 0;
  accent-color: var(--accent);
}
.bm-mixer {
  flex: 0 0 auto;
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 12px 14px 14px;
  border-top: 1px solid var(--border);
  background: color-mix(in srgb, var(--bg) 38%, var(--surface));
}
.bm-toast {
  position: absolute;
  left: 16px;
  right: 16px;
  bottom: max(16px, env(safe-area-inset-bottom));
  z-index: 40;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 44px;
  padding: 10px 14px;
  border-radius: 12px;
  border: 1px solid var(--danger);
  background: var(--surface);
  color: var(--text);
  font-size: 13px;
  font-weight: 650;
  box-shadow: 0 8px 24px rgba(0,0,0,0.28);
}
.bm-sync-pill {
  display: inline-flex;
  align-items: center;
  min-height: 30px;
  padding: 6px 11px;
  border-radius: 999px;
  border: 1px solid var(--border);
  color: var(--muted);
  background: var(--bg);
  font-size: 11px;
  font-weight: 700;
}

/* mobius-ui:Focus - app-owned. */
:where(button, a, input, textarea, select, summary, [role="button"], [tabindex]:not([tabindex="-1"])):focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
/* /mobius-ui:Focus */

@media (max-width: 740px) {
  .bm-header {
    align-items: flex-start;
  }
  .bm-main {
    grid-template-columns: 1fr;
  }
  .bm-panel {
    min-height: 360px;
  }
  .bm-scroll {
    padding: 12px 12px 18px;
  }
}

@media (max-width: 420px) {
  .bm-pad-grid {
    gap: 6px;
  }
  .bm-pad {
    min-height: 62px;
    padding: 8px;
    border-radius: 10px;
  }
  .bm-pad-name {
    font-size: 12px;
  }
  .bm-pad-kind,
  .bm-pad-index {
    font-size: 10px;
  }
  .bm-detail {
    padding: 12px;
  }
  .bm-slider-row {
    grid-template-columns: 58px minmax(0, 1fr) 36px;
  }
}

/* mobius-ui:ReducedMotion - app-owned. */
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
