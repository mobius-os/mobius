// CANONICAL DIFF VIEWER: copy this entire folder verbatim. It imports only
// React and its own flat sibling modules. Styles ship as a JavaScript string
// because the mini-app compiler rejects CSS side-output.

export const DIFF_VIEWER_STYLES = `
.diff-view {
  width: 100%;
  min-width: 0;
  box-sizing: border-box;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--bg);
  color: var(--text);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.5;
}

.diff-view:focus-visible {
  outline: 2px solid var(--text);
  outline-offset: -2px;
}

.diff-view__content {
  width: 100%;
  min-width: 0;
}

.diff-view__hunk + .diff-view__hunk {
  border-top: 1px solid var(--border);
}

.diff-view__hunk-header {
  display: grid;
  grid-template-columns: 8ch minmax(0, 1fr);
  min-width: 0;
  background: var(--surface-2);
  color: var(--muted);
}

.diff-view__hunk-header code {
  padding: 5px 10px;
  font: inherit;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.diff-view__hunk-gutter {
  border-right: 1px solid var(--border);
}

.diff-view__line {
  display: grid;
  grid-template-columns: 7ch 1.6ch minmax(0, 1fr);
  min-width: 0;
}

.diff-view__line--add {
  background: color-mix(in srgb, var(--green) 11%, transparent);
}

.diff-view__line--del {
  background: color-mix(in srgb, var(--danger) 11%, transparent);
}

.diff-view__line--context {
  color: color-mix(
    in srgb,
    var(--text) 78%,
    var(--muted)
  );
}

.diff-view__line--meta {
  color: var(--muted);
  font-style: italic;
}

.diff-view__numbers {
  display: grid;
  grid-template-columns: repeat(2, 3.5ch);
  color: var(--muted);
  background: color-mix(
    in srgb,
    var(--surface-2) 58%,
    transparent
  );
  border-right: 1px solid var(--border);
  font-variant-numeric: tabular-nums;
  user-select: none;
}

.diff-view__line-number {
  min-width: 0;
  box-sizing: border-box;
  padding: 1px 3px;
  text-align: right;
}

.diff-view__line-number + .diff-view__line-number {
  border-left: 1px solid color-mix(
    in srgb,
    var(--border) 60%,
    transparent
  );
}

.diff-view__sign {
  padding: 1px 0;
  color: var(--muted);
  text-align: center;
  user-select: none;
}

.diff-view__line--add .diff-view__sign {
  color: var(--green);
}

.diff-view__line--del .diff-view__sign {
  color: var(--danger);
}

.diff-view__line-text {
  padding: 1px 10px 1px 2px;
  font: inherit;
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}

.diff-view--message {
  padding: 8px 10px;
  color: var(--muted);
}

.file-diff-list {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg);
  color: var(--text);
}

.file-diff-list__files {
  display: flex;
  flex-direction: column;
  margin: 0;
  padding: 0;
  list-style: none;
}

.file-diff-list__item + .file-diff-list__item,
.file-diff-list__more {
  border-top: 1px solid var(--border);
}

.file-diff-list__row {
  display: flex;
  align-items: center;
  gap: clamp(6px, 2.5vw, 9px);
  width: 100%;
  min-height: 44px;
  padding: 8px clamp(8px, 3vw, 12px);
  border: 0;
  background: transparent;
  color: var(--text);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

@media (hover: hover) and (pointer: fine) {
  .file-diff-list__row:hover,
  .file-diff-list__more:hover {
    background: color-mix(
      in srgb,
      var(--surface-2) 72%,
      transparent
    );
  }
}

.file-diff-list__row:focus-visible,
.file-diff-list__more:focus-visible {
  position: relative;
  z-index: 1;
  outline: 2px solid var(--text);
  outline-offset: -2px;
}

.file-diff-list__caret {
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  border-right: 1.5px solid var(--muted);
  border-bottom: 1.5px solid var(--muted);
  transform: rotate(-45deg);
  transition: transform 0.15s ease-out;
}

.file-diff-list__row[aria-expanded="true"] .file-diff-list__caret {
  transform: rotate(45deg);
}

.file-diff-list__path {
  display: flex;
  flex: 1 1 auto;
  align-items: baseline;
  min-width: 0;
  overflow: hidden;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.3;
}

.file-diff-list__dir {
  /* Content-sized, and the FIRST thing to yield room so the basename stays
     readable. The 2ch floor keeps the truncation ellipsis visible ("…s/") — at
     0 a squeezed directory vanishes and strands a bare "/" that reads as an
     absolute path. Cost: a single-character directory is padded by ~1ch. */
  flex: 0 999 auto;
  min-width: 2ch;
  overflow: hidden;
  color: var(--muted);
  direction: rtl;
  text-align: left;
  text-overflow: ellipsis;
  unicode-bidi: isolate;
  white-space: nowrap;
}

.file-diff-list__separator {
  flex: 0 0 auto;
  color: var(--muted);
}

.file-diff-list__basename {
  /* Content-sized and shrink-resistant: the basename is the part that must stay
     readable, so it only ellipsizes once the directory has fully collapsed. */
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--text);
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.file-diff-list__meta {
  display: inline-flex;
  flex: 0 0 auto;
  align-items: baseline;
  justify-content: flex-end;
  gap: 8px;
}

.file-diff-list__kind {
  padding: 2px 5px;
  border-radius: 5px;
  background: color-mix(
    in srgb,
    var(--surface-2) 80%,
    transparent
  );
  color: var(--muted);
  font-size: 10.5px;
  line-height: 1.2;
}

.file-diff-list__stat {
  display: inline-flex;
  align-items: baseline;
  justify-content: flex-end;
  gap: 6px;
  min-width: 6.5ch;
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.file-diff-list__add {
  color: var(--green);
  font-weight: 600;
}

.file-diff-list__delete {
  color: var(--danger);
  font-weight: 600;
}

.file-diff-list__panel {
  /* One vertical reading surface: long code wraps instead of creating a
     second horizontal navigation problem inside an already-scrollable pane. */
  max-height: min(64dvh, 760px);
  min-width: 0;
  overflow-x: hidden;
  overflow-y: auto;
  border-top: 1px solid var(--border);
  background: var(--surface-2);
}

.file-diff-list__message,
.file-diff-list__note {
  margin: 0;
  padding: 9px 12px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.45;
}

.file-diff-list__note {
  border-top: 1px solid var(--border);
}

@media (max-width: 520px) {
  .diff-view {
    font-size: 11px;
  }

  .diff-view__hunk-header {
    grid-template-columns: 6ch minmax(0, 1fr);
  }

  .diff-view__line {
    grid-template-columns: 5.5ch 1.4ch minmax(0, 1fr);
  }

  .diff-view__numbers {
    grid-template-columns: repeat(2, 2.75ch);
  }

  .diff-view__line-number {
    padding-inline: 2px;
  }

  .diff-view__line-text {
    padding-right: 6px;
  }
}

.file-diff-list__more {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 100%;
  min-height: 44px;
  padding: 8px 12px;
  border-right: 0;
  border-bottom: 0;
  border-left: 0;
  background: transparent;
  color: var(--text);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
}

@media (prefers-reduced-motion: reduce) {
  .file-diff-list__caret {
    transition: none;
  }
}
`

const STYLE_ELEMENT_ID = 'mobius-diff-viewer-styles'

/** Install the canonical stylesheet once; safe during SSR and pre-rendering. */
export function ensureDiffViewerStyles() {
  if (typeof document === 'undefined') return null
  const existing = document.getElementById(STYLE_ELEMENT_ID)
  if (existing) return existing
  const parent = document.head || document.documentElement
  if (!parent) return null
  const style = document.createElement('style')
  style.id = STYLE_ELEMENT_ID
  style.textContent = DIFF_VIEWER_STYLES
  parent.appendChild(style)
  return style
}
