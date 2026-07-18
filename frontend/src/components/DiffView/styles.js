// CANONICAL DIFF VIEWER: copy this entire folder verbatim. It imports only
// React and its own flat sibling modules. Styles ship as a JavaScript string
// because the mini-app compiler rejects CSS side-output.

export const DIFF_VIEWER_STYLES = `
.diff-view {
  width: max-content;
  min-width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--border, #2a2a2a);
  border-radius: 8px;
  background: var(--bg, #0d0d0d);
  color: var(--text, #ececec);
  font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-size: 11.5px;
  line-height: 1.5;
}

.diff-view:focus-visible {
  outline: 2px solid var(--text, #ececec);
  outline-offset: -2px;
}

.diff-view__content {
  width: 100%;
  min-width: max-content;
}

.diff-view__hunk + .diff-view__hunk {
  border-top: 1px solid var(--border, #2a2a2a);
}

.diff-view__hunk-header {
  display: grid;
  grid-template-columns: 16ch minmax(max-content, 1fr);
  min-width: max-content;
  background: var(--surface2, #212121);
  color: var(--muted, #a8a8a8);
}

.diff-view__hunk-header code {
  padding: 5px 10px;
  font: inherit;
  white-space: pre;
}

.diff-view__hunk-gutter {
  border-right: 1px solid var(--border, #2a2a2a);
}

.diff-view__line {
  display: grid;
  grid-template-columns: 14ch 2ch minmax(max-content, 1fr);
  min-width: max-content;
}

.diff-view__line--add {
  background: color-mix(in srgb, var(--green, #16a34a) 11%, transparent);
}

.diff-view__line--del {
  background: color-mix(in srgb, var(--danger, #ef4444) 11%, transparent);
}

.diff-view__line--context {
  color: color-mix(
    in srgb,
    var(--text, #ececec) 78%,
    var(--muted, #a8a8a8)
  );
}

.diff-view__line--meta {
  color: var(--muted, #a8a8a8);
  font-style: italic;
}

.diff-view__numbers {
  display: grid;
  grid-template-columns: repeat(2, 7ch);
  color: var(--muted, #a8a8a8);
  background: color-mix(
    in srgb,
    var(--surface2, #212121) 58%,
    transparent
  );
  border-right: 1px solid var(--border, #2a2a2a);
  font-variant-numeric: tabular-nums;
  user-select: none;
}

.diff-view__line-number {
  min-width: 0;
  box-sizing: border-box;
  padding: 1px 5px;
  text-align: right;
}

.diff-view__line-number + .diff-view__line-number {
  border-left: 1px solid color-mix(
    in srgb,
    var(--border, #2a2a2a) 60%,
    transparent
  );
}

.diff-view__sign {
  padding: 1px 0;
  color: var(--muted, #a8a8a8);
  text-align: center;
  user-select: none;
}

.diff-view__line--add .diff-view__sign {
  color: var(--green, #16a34a);
}

.diff-view__line--del .diff-view__sign {
  color: var(--danger, #ef4444);
}

.diff-view__line-text {
  padding: 1px 10px 1px 2px;
  font: inherit;
  white-space: pre;
}

.diff-view--message {
  padding: 8px 10px;
  color: var(--muted, #a8a8a8);
}

.file-diff-list {
  min-width: 0;
  overflow: hidden;
  border: 1px solid var(--border, #2a2a2a);
  border-radius: 10px;
  background: var(--bg, #0d0d0d);
  color: var(--text, #ececec);
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
  border-top: 1px solid var(--border, #2a2a2a);
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
  color: var(--text, #ececec);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

@media (hover: hover) and (pointer: fine) {
  .file-diff-list__row:hover,
  .file-diff-list__more:hover {
    background: color-mix(
      in srgb,
      var(--surface2, #212121) 72%,
      transparent
    );
  }
}

.file-diff-list__row:focus-visible,
.file-diff-list__more:focus-visible {
  position: relative;
  z-index: 1;
  outline: 2px solid var(--text, #ececec);
  outline-offset: -2px;
}

.file-diff-list__caret {
  flex: 0 0 auto;
  width: 6px;
  height: 6px;
  border-right: 1.5px solid var(--muted, #a8a8a8);
  border-bottom: 1.5px solid var(--muted, #a8a8a8);
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
  color: var(--muted, #a8a8a8);
  direction: rtl;
  text-align: left;
  text-overflow: ellipsis;
  unicode-bidi: isolate;
  white-space: nowrap;
}

.file-diff-list__separator {
  flex: 0 0 auto;
  color: var(--muted, #a8a8a8);
}

.file-diff-list__basename {
  /* Content-sized and shrink-resistant: the basename is the part that must stay
     readable, so it only ellipsizes once the directory has fully collapsed. */
  flex: 0 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--text, #ececec);
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
    var(--surface2, #212121) 80%,
    transparent
  );
  color: var(--muted, #a8a8a8);
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
  color: var(--green, #16a34a);
  font-weight: 600;
}

.file-diff-list__delete {
  color: var(--danger, #ef4444);
  font-weight: 600;
}

.file-diff-list__panel {
  /* Bound one expanded file so a long diff cannot displace the modal actions. */
  max-height: 340px;
  min-width: 0;
  overflow: auto;
  overscroll-behavior: contain;
  border-top: 1px solid var(--border, #2a2a2a);
  background: var(--surface2, #212121);
}

.file-diff-list__message,
.file-diff-list__note {
  margin: 0;
  padding: 9px 12px;
  color: var(--muted, #a8a8a8);
  font-size: 12px;
  line-height: 1.45;
}

.file-diff-list__note {
  min-width: max-content;
  border-top: 1px solid var(--border, #2a2a2a);
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
  color: var(--text, #ececec);
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
