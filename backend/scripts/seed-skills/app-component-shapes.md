# Mini-app component shapes

The canonical shape for every recurring piece of mini-app UI — markup +
scoped CSS — so each app holds its OWN copy of a consistent block. Copy the
block you need into your app's `const CSS`, keep the class names + structure
identical, and diverge only where your app genuinely needs to. `Read` this
when building or restyling any app's UI, alongside [building-apps.md].

Why copies and not a shared library yet: a shared component freezes an API
before the shapes have proven stable, and then any app that needs something
the component didn't anticipate hits a wall. Copies let each app diverge its
own CSS freely — full CSS power, no permission, no blast radius. When ~3 apps
carry a byte-identical fenced block, that block has earned extraction into a
real `@mobius/ui` and the lift is mechanical (the names + markup already
match). Until then, consistent copies are correct. This is the platform's
"code empowers the agent; it does not police it," in CSS form.

## The rules (read once, then copy blocks)

- **One stylesheet, not inline objects.** Declare a module-level
  ``const CSS = `...` `` and render it once at the app root as
  `<style>{CSS}</style>`. Use the inline `style={}` prop ONLY for values
  computed at render time (a measured height, a drag transform, a per-row
  accent). Inline objects can't do `:hover`/`:focus`/`:active`, media
  queries, `@keyframes`, or pseudo-elements — that's the friction wall this
  avoids. The app runs in its own iframe, so the `<style>` is automatically
  scoped to your app; no CSS Modules, no hashing, no BEM-for-isolation.
- **Naming:** a short per-app prefix on every class (`mg-` mind, `cb-` atlas,
  a 2–3-char mnemonic for yours — `ma-` is the placeholder below) + semantic
  kebab role names (`ma-header`, `ma-sheet`, `ma-card`, `ma-btn`). States use
  REAL pseudo-classes (`.ma-btn:hover`, `:disabled`, `:focus-visible`).
  App-driven state CSS can't read uses an `is-`/`has-` modifier class
  (`.ma-card.is-selected`). **Never** a `tab(active)` / `card(variant)`
  JS-helper that returns a style object — that hides state in JS and blocks
  extraction.
- **Structural color is always a theme token** so the app follows light/dark:
  `--bg --surface --surface2 --text --muted --accent --accent-fg --accent-hover
  --accent-dim --border --border-light --danger --green --font --mono`. There
  is **no `--red`** (use `--danger`) and **no `--fg`** (use `--text`).
  Hardcoded hex only for an app-specific accent the theme can't express.
- **`--accent-fg` is the ONLY legal foreground on an accent/danger FILL**
  (a `.ma-btn-primary`, a `.ma-btn-danger`, an accent chip). It resolves a
  prior three-way split — apps had been hardcoding `#fff` / `#0d0d0d` /
  `#062016` for that foreground, so a custom theme broke one of them. Write
  `color: var(--accent-fg)` with **NO fallback hex** (`var(--accent-fg, #fff)`
  re-introduces the exact split the token exists to kill). Never hardcode the
  foreground on a fill.
- **Touch + radius:** every interactive control `min-height: 44px`; icon-only
  buttons get an `aria-label`. Radius scale: 8px inputs/small, 10–12px
  cards/primary buttons, 16px sheet top.
- **Hard pre-ship checklist (don't skip — these are the gaps a grep found
  recur in every app):**
  - Every focusable input is `font-size: 16px` (anything smaller triggers
    iOS Safari zoom-on-focus). Don't go lower on a field the user can tap into.
  - Every tap target is `>= 44px`. A thin control (a resizer, a drag handle)
    stays thin VISUALLY but gets a fat invisible hit-area (a transparent
    `::before`/`::after` or padding that pushes the hit-box to 44px).
  - No bare `outline: none` on an interactive control — keep a visible
    `:focus-visible` ring (see the Focus shape) or the keyboard user is lost.
  - One `mobius-ui:ReducedMotion` block per app (below); every `@keyframes`
    animation also has a `prefers-reduced-motion` escape.
  - Edge-pinned surfaces respect `env(safe-area-inset-*)` (below).
- **No native `confirm/alert/prompt`** — the sandbox has no `allow-modals`,
  so they silently no-op. Use the bottom-sheet (§3).
- The app-frame already injects a global reset + the theme `:root`. **Do not
  redeclare a reset.**
- **Keep copies in sync with fence comments.** Wrap each shared block in an
  IDENTICAL versioned marker, so a `grep` finds every app on a given shape
  and a future extraction is mechanical:
  `/* mobius-ui:Header v1 — keep in sync; library candidate. Diverge below the marker only. */`
  … `/* /mobius-ui:Header */`. Keep the class names + markup inside a fence
  identical across apps; diverge the *shape* when you must, never *rename*
  shared classes gratuitously. Keep blocks in a stable order: root → header →
  list/feed → cards → empty → sheet → buttons/inputs → animations/scrollbar.

`app-latex` and `mind` are the cleanest on-standard references.

---

## 1. App shell root (`ma-root`)

```jsx
<div className="ma-root">
  <style>{CSS}</style>
  <header className="ma-header">…</header>
  <div className="ma-scroll">…</div>
  {/* sheets/toasts render here, last */}
</div>
```

```css
/* mobius-ui:Root v1 — keep in sync; library candidate. Diverge below the marker only. */
.ma-root {
  position: relative;        /* anchor for scrims / sheets / toasts (they're absolute, not fixed) */
  display: flex; flex-direction: column;
  height: 100%; width: 100%; max-width: 100%;
  overflow: hidden;          /* inner .ma-scroll owns vertical scroll */
  background: var(--bg); color: var(--text); font-family: var(--font);
  -webkit-font-smoothing: antialiased;
}
.ma-scroll {
  flex: 1; min-height: 0;    /* the flexbox-overflow fix — REQUIRED so children scroll */
  overflow-y: auto; overflow-x: hidden;
  padding: 14px 16px 32px;
  word-break: break-word; overflow-wrap: anywhere;
}
/* /mobius-ui:Root */
```

Diverge on padding, a desktop `max-width` cap, or (rarely, for a full-bleed
canvas like a map) `position: fixed; inset: 0`. `min-height: 0` on the scroll
child is non-negotiable.

**Safe area:** a full-bleed root (one that paints to the device edges — a
`position: fixed; inset: 0` canvas, or a root with no edge-pinned header/sheet
chrome of its own) must inset its content for the notch and home indicator:
`padding: env(safe-area-inset-top) env(safe-area-inset-right)
env(safe-area-inset-bottom) env(safe-area-inset-left)`. A normal root whose
header and bottom sheet already handle their own edges (below) does NOT need
this — only add safe-area on the surface that actually touches an edge.

---

## 2. Header (`ma-header`) — brand cluster + right-side SLOT

```jsx
<header className="ma-header">
  <div className="ma-brand">
    <span className="ma-mark" aria-hidden="true">{/* glyph, letter, or dot */}</span>
    <div className="ma-brand-text">
      <h1 className="ma-title">Atlas</h1>
      <span className="ma-subtitle">12 of 195 countries visited</span>
    </div>
  </div>
  <div className="ma-header-right">{/* tabs / segmented / badge / sync pill */}</div>
</header>
```

```css
/* mobius-ui:Header v1 — keep in sync; library candidate. Diverge below the marker only. */
.ma-header {
  flex: 0 0 auto;
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  min-height: 48px; padding: 12px 16px;
  background: var(--surface); border-bottom: 1px solid var(--border);
}
.ma-brand { display: flex; align-items: center; gap: 11px; min-width: 0; }
.ma-mark {
  flex: 0 0 auto; width: 30px; height: 30px; border-radius: 9px;  /* 50% circle is an allowed variant */
  display: flex; align-items: center; justify-content: center;
  background: color-mix(in srgb, var(--accent) 16%, transparent);
  color: var(--accent); font-size: 16px; font-weight: 700; line-height: 1;
}
.ma-brand-text { min-width: 0; line-height: 1.15; }
.ma-title { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: -0.015em; }
.ma-subtitle {
  display: block; margin-top: 2px; font-size: 12px; font-weight: 500; color: var(--muted);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-variant-numeric: tabular-nums;
}
.ma-header-right { display: flex; align-items: center; gap: 8px; flex: 0 0 auto; }
/* /mobius-ui:Header */
```

The right side is a SLOT — drop your tabs / toggle / badge / sync pill in.
The mark may be omitted entirely (title + subtitle alone is valid). Don't
diverge on the flex/space-between skeleton or the 48px min-height.

**Safe area:** a top-pinned header (one the root does NOT inset for you) keeps
its content clear of the notch by folding the inset into its top padding:
`padding-top: max(12px, env(safe-area-inset-top))` (keep the existing `12px`
as the floor). Only the header that actually sits at the top edge needs this.

---

## 3. Bottom-sheet modal + scrim (`ma-sheet` / `ma-scrim`) — the dialog

```jsx
{open && (
  <div className="ma-scrim" onClick={busy ? null : onCancel}
       role="dialog" aria-modal="true" aria-label="Confirm">
    <div className="ma-sheet" onClick={(e) => e.stopPropagation()}>
      <h3 className="ma-sheet-title">Uninstall {app.name}?</h3>
      <p className="ma-sheet-body">This removes the app and its stored data.</p>
      {/* optional <input className="ma-input" …/> for a prompt-style sheet */}
      <div className="ma-sheet-actions">
        <button className="ma-btn ma-btn-secondary" onClick={onCancel} disabled={busy}>Cancel</button>
        <button className="ma-btn ma-btn-danger" onClick={onConfirm} disabled={busy}>
          {busy ? 'Removing…' : 'Uninstall'}
        </button>
      </div>
    </div>
  </div>
)}
```

```css
/* mobius-ui:Sheet v1 — keep in sync; library candidate. Diverge below the marker only. */
.ma-scrim {
  position: absolute; inset: 0; z-index: 100;   /* absolute → stays inside the app, never over shell chrome */
  display: flex; align-items: flex-end; justify-content: center;  /* bottom sheet; center is a variant */
  padding: 16px; background: rgba(0, 0, 0, 0.5);
}
.ma-sheet {
  width: 100%; max-width: 480px; max-height: 85vh; overflow-y: auto;
  padding: 24px; background: var(--surface); border: 1px solid var(--border);
  border-radius: 16px 16px 0 0; box-shadow: 0 -8px 32px rgba(0, 0, 0, 0.3);
}
.ma-sheet-title { margin: 0 0 12px; font-size: 16px; font-weight: 700; letter-spacing: -0.01em; }
.ma-sheet-body { margin: 0 0 16px; font-size: 14px; line-height: 1.5; color: var(--muted); }
.ma-sheet-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 24px; }
.ma-sheet-actions .ma-btn { flex: 1; }
/* /mobius-ui:Sheet */
```

The one allowed structural divergence is `align-items: center` + all-corner
radius for a tiny centered confirm. Keep the scrim, `stopPropagation`,
`aria-modal`, and the flex:1 action row.

**Safe area:** a bottom-pinned sheet (and any other bottom-pinned surface — a
floating pill, a docked composer) keeps its controls above the home indicator
with `padding-bottom: max(24px, env(safe-area-inset-bottom))` (keep the
shape's base padding as the floor). Only bottom-edge surfaces need it; the
scrim itself, being full-bleed, does not.

---

## 4. Empty state (`ma-empty`) — mark + title + subtitle

```jsx
<div className="ma-empty">
  <div className="ma-empty-mark" aria-hidden="true">🌙</div>
  <div className="ma-empty-title">No briefs yet</div>
  <p className="ma-empty-text">Dreaming runs overnight. Your first morning brief will be waiting here.</p>
</div>
```

```css
/* mobius-ui:Empty v1 — keep in sync; library candidate. Diverge below the marker only. */
.ma-empty {
  display: flex; flex-direction: column; align-items: center; text-align: center; gap: 8px;
  max-width: 440px; margin: 0 auto; padding: 48px 24px; color: var(--muted);
}
.ma-empty-mark {
  width: 64px; height: 64px; margin-bottom: 10px; border-radius: 18px;
  display: flex; align-items: center; justify-content: center; font-size: 30px; line-height: 1;
  background: color-mix(in srgb, var(--accent) 14%, transparent);
  border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--border));
}
.ma-empty-title { font-size: 17px; font-weight: 700; color: var(--text); letter-spacing: -0.01em; }
.ma-empty-text { margin: 0; font-size: 14px; line-height: 1.6; }
/* /mobius-ui:Empty */
```

Every list / feed / graph gets one — never a bare "Nothing here." The mark
tile is optional (drop it for a text-only empty). Keep the centered column +
the title/text scale.

---

## 5. Card / list item (`ma-card`)

```jsx
<button className={`ma-card${isLatest ? ' is-featured' : ''}`} onClick={() => onOpen(d)}>
  <div className="ma-card-main">
    <div className="ma-card-title">{title}</div>
    <div className="ma-card-sub">{sub}</div>
  </div>
  <span className="ma-card-chevron" aria-hidden="true">›</span>
</button>
```

```css
/* mobius-ui:Card v1 — keep in sync; library candidate. Diverge below the marker only. */
.ma-card {
  display: flex; align-items: center; gap: 14px; width: 100%; min-height: 44px;
  padding: 15px 16px; text-align: left;
  background: var(--surface); color: var(--text);
  border: 1px solid var(--border); border-radius: 12px; font-family: var(--font);
  transition: border-color 0.16s ease, transform 0.12s ease, background 0.16s ease;
}
button.ma-card { cursor: pointer; }
button.ma-card:hover { border-color: color-mix(in srgb, var(--accent) 60%, var(--border)); }
button.ma-card:active { transform: scale(0.992); }
button.ma-card:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ma-card.is-featured { border-left: 3px solid var(--accent); }       /* app-driven state = modifier class */
.ma-card.is-selected { background: color-mix(in srgb, var(--accent) 12%, var(--surface)); }
.ma-card-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.ma-card-title { font-size: 16px; font-weight: 700; letter-spacing: -0.01em; }
.ma-card-sub { font-size: 12px; font-weight: 500; color: var(--muted); }
.ma-card-chevron { flex: 0 0 auto; font-size: 20px; line-height: 1; color: var(--muted); opacity: 0.7; }
/* /mobius-ui:Card */
```

Static container cards drop the `button` pseudo-states + chevron. State
(featured/selected/error) rides `is-`/`has-` modifier classes — never a
`card(variant)` JS helper.

---

## 6. Buttons (`ma-btn` + `-primary` / `-secondary` / `-ghost` / `-danger` / `-icon`)

```jsx
<button className="ma-btn ma-btn-primary" onClick={save} disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
<button className="ma-btn ma-btn-secondary" onClick={cancel}>Cancel</button>
<button className="ma-btn ma-btn-ghost" onClick={skip}>Skip</button>
<button className="ma-btn ma-btn-danger" onClick={remove}>Delete</button>
<button className="ma-btn ma-btn-icon" aria-label="Close" onClick={close}>×</button>
```

```css
/* mobius-ui:Button v1 — keep in sync; library candidate. Diverge below the marker only. */
.ma-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  min-height: 44px; padding: 10px 16px; border-radius: 10px;
  border: 1px solid var(--border); background: var(--surface); color: var(--text);
  font-family: var(--font); font-size: 14px; font-weight: 600; cursor: pointer; white-space: nowrap;
  transition: background 0.14s ease, border-color 0.14s ease, transform 0.1s ease;
}
.ma-btn:active { transform: scale(0.97); }
.ma-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ma-btn:disabled { opacity: 0.5; cursor: default; transform: none; }
.ma-btn-primary { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); }
.ma-btn-primary:hover { filter: brightness(1.06); }
.ma-btn-secondary { background: var(--surface2, var(--surface)); }
.ma-btn-secondary:hover { border-color: color-mix(in srgb, var(--accent) 40%, var(--border)); }
.ma-btn-ghost { background: transparent; border-color: transparent; color: var(--accent); }
.ma-btn-ghost:hover { background: color-mix(in srgb, var(--accent) 10%, transparent); }
.ma-btn-danger { background: var(--danger); border-color: var(--danger); color: var(--accent-fg); }
.ma-btn-icon { width: 44px; padding: 0; border-radius: 8px; font-size: 18px; }   /* icon-only → needs aria-label */
/* /mobius-ui:Button */
```

The highest-value shared shape. The fill foreground is `var(--accent-fg)` —
the shell sets it to white for the default purple accent, and a custom theme
that goes light should set `--accent-fg` to a dark value (this replaces the
old per-app `color: <dark>` override; theme it once at the token, not per app).
A full-width form-submit adds `width: 100%` via a `.ma-btn-block` modifier.

---

## 7. Input / textarea (`ma-input` / `ma-textarea`)

```jsx
<input className="ma-input" value={v} onChange={(e) => set(e.target.value)} placeholder="…" />
<textarea className="ma-textarea" value={t} onChange={(e) => set(e.target.value)} />
```

```css
/* mobius-ui:Input v1 — keep in sync; library candidate. Diverge below the marker only. */
.ma-input, .ma-textarea {
  display: block; width: 100%; box-sizing: border-box; min-height: 44px; padding: 11px 12px;
  background: var(--surface); color: var(--text); border: 1px solid var(--border);
  border-radius: 8px; outline: none; font-family: var(--font);
  font-size: 16px;           /* 16px stops iOS Safari zoom-on-focus — don't go lower on a focusable field */
  line-height: 1.5; transition: border-color 0.15s ease, box-shadow 0.15s ease;
}
.ma-input::placeholder, .ma-textarea::placeholder { color: var(--muted); }
.ma-input:focus, .ma-textarea:focus { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
.ma-textarea { min-height: 120px; resize: vertical; }
.ma-input-mono { font-family: var(--mono); }   /* code / URL fields */
/* /mobius-ui:Input */
```

---

## 8. Segmented control / tabs (`ma-seg`) — one shape, two active styles

```jsx
<div className="ma-seg" role="tablist" aria-label="View mode">
  <button role="tab" aria-selected={view === 'graph'}
          className={`ma-seg-btn${view === 'graph' ? ' is-active' : ''}`}
          onClick={() => setView('graph')}>Graph</button>
  <button role="tab" aria-selected={view === 'list'}
          className={`ma-seg-btn${view === 'list' ? ' is-active' : ''}`}
          onClick={() => setView('list')}>List</button>
</div>
```

```css
/* mobius-ui:Segmented v1 — keep in sync; library candidate. Diverge below the marker only. */
.ma-seg {
  display: inline-flex; gap: 2px; padding: 3px;
  background: var(--surface2, var(--surface)); border: 1px solid var(--border); border-radius: 10px;
}
.ma-seg-btn {
  min-height: 44px; padding: 6px 14px; border: 0; border-radius: 7px;
  background: transparent; color: var(--muted); font-family: var(--font);
  font-size: 13px; font-weight: 650; cursor: pointer; transition: background 0.15s, color 0.15s;
}
.ma-seg-btn:hover { color: var(--text); }
.ma-seg-btn.is-active { background: var(--bg); color: var(--text); box-shadow: 0 1px 3px rgba(0, 0, 0, 0.18); }
.ma-seg.is-accent .ma-seg-btn.is-active { background: var(--accent); color: var(--accent-fg); box-shadow: none; }
/* /mobius-ui:Segmented */
```

Default active is the subtle raised chip (theme-safe on any accent). Opt into
`.ma-seg.is-accent` for the bold accent fill. Add `role="tablist"` +
`aria-selected` when it switches views; `flex: 1` per button for a full-width
tab bar.

---

## 9. Agent-chat mount (`ma-chat-embed`)

The wrapper that holds `window.mobius.chat(...)`. The load-bearing rule:
`min-height: 0` (flex panel) or an explicit height (box), `overflow: hidden`,
and the iframe fills it. See the [building-apps.md] "Agent-powered mini-apps"
section for the one-call helper (`persist` + `onTurnDone`).

```css
/* mobius-ui:ChatEmbed v1 — keep in sync; library candidate. Diverge below the marker only. */
.ma-chat-embed {
  flex: 1 1 auto; min-height: 0;   /* the flexbox-overflow fix — lets the iframe scroll internally */
  overflow: hidden; background: var(--bg);
}
.ma-chat-embed iframe { display: block; width: 100%; height: 100%; border: 0; }
/* Fixed-height box instead of a flex panel:
   .ma-chat-embed { flex: none; height: 460px; border: 1px solid var(--border); border-radius: 10px; } */
/* /mobius-ui:ChatEmbed */
```

---

## 10. Smaller recurring blocks

```css
/* mobius-ui:Focus v1 — keep in sync; library candidate. Required once per app. */
/* A visible keyboard-focus ring on every interactive control (WCAG 2.4.7).
   :focus-visible only shows for keyboard nav, so mouse/touch taps stay clean.
   Per-control shapes (.ma-btn, .ma-card) already carry their own ring; this is
   the catch-all for anything that doesn't. */
:where(button, a, input, textarea, select, summary, [role="button"],
       [tabindex]:not([tabindex="-1"])):focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
}
/* Never ship a bare `outline: none` on an interactive control — it strips the
   ring for keyboard users with no replacement. The ONLY allowed suppression is
   `:focus:not(:focus-visible) { outline: none }`, and only when a custom
   :focus-visible style already exists to replace it. */
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

/* mobius-ui:SyncPill v1 — keep in sync; library candidate. */
.ma-sync-pill {  /* floating offline/pending indicator; hidden when online + 0 pending */
  position: absolute; right: 12px; bottom: 12px; z-index: 40;
  display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 999px;
  background: var(--surface); border: 1px solid var(--border); color: var(--muted);
  font-size: 11px; font-weight: 600; font-variant-numeric: tabular-nums; box-shadow: 0 2px 8px rgba(0,0,0,0.18);
}
.ma-sync-pill-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--muted); }
.ma-sync-pill.is-pending .ma-sync-pill-dot { background: var(--accent); box-shadow: 0 0 0 3px color-mix(in srgb, var(--accent) 22%, transparent); }
.ma-sync-pill.is-offline { border-color: var(--accent); color: var(--accent); }
/* /mobius-ui:SyncPill */

/* mobius-ui:Spinner v1 — keep in sync; library candidate. */
@keyframes ma-spin { to { transform: rotate(360deg); } }
.ma-spinner {
  width: 26px; height: 26px; border-radius: 50%;
  border: 2.5px solid color-mix(in srgb, var(--accent) 18%, transparent); border-top-color: var(--accent);
  animation: ma-spin 0.8s linear infinite;
}
@media (prefers-reduced-motion: reduce) { .ma-spinner { animation: none; } }   /* mandatory */
/* /mobius-ui:Spinner */

/* mobius-ui:Toast v1 — keep in sync; library candidate. */
.ma-toast {
  position: absolute; left: 16px; right: 16px; bottom: 16px; z-index: 200;   /* absolute → inside the app */
  display: flex; align-items: center; gap: 12px; padding: 12px 16px;
  background: var(--surface); border: 1px solid var(--accent); border-radius: 12px;
  font-size: 14px; line-height: 1.5; box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}
.ma-toast.is-success { border-color: var(--green); }
.ma-toast.is-error { border-color: var(--danger); }
/* /mobius-ui:Toast */

/* mobius-ui:SectionHead v1 — keep in sync; library candidate. */
.ma-section-head { display: flex; align-items: center; gap: 10px; margin: 0 0 8px; }
.ma-section-icon {
  width: 30px; height: 30px; flex: 0 0 auto; border-radius: 9px;
  display: flex; align-items: center; justify-content: center;
  background: color-mix(in srgb, var(--accent) 14%, transparent); font-size: 15px;
}
.ma-section-label { margin: 0; font-size: 14.5px; font-weight: 700; letter-spacing: -0.01em; }
/* /mobius-ui:SectionHead */

/* mobius-ui:Scrollskin v1 — keep in sync; library candidate. Add the `ma-scroll` class to a scroller. */
.ma-scroll::-webkit-scrollbar { width: 9px; height: 9px; }
.ma-scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 999px; border: 2px solid transparent; background-clip: padding-box; }
.ma-scroll::-webkit-scrollbar-thumb:hover { background: var(--muted); background-clip: padding-box; }
.ma-scroll::-webkit-scrollbar-track { background: transparent; }
/* /mobius-ui:Scrollskin */
```

No FAB shape is specced — zero apps use a floating action button today.
Compose affordances live inline in a header or list. If a future app needs
one, build it from the `.ma-btn-primary` look + the sync-pill's floating
mechanics, with an `aria-label`.

---

## 9. ChatSplit — embedded chat panel with pill ↔ split ↔ full state machine

`window.mobius.split(opts)` owns the drag handle, state machine, and
`sessionStorage` persistence. Your mount element needs CSS that reads the two
custom properties the helper sets:

- **`--cs-content-h`** — content-pane height in px (portrait / vertical split)
- **`--cs-content-w`** — content-pane width in px (side / horizontal split, ≥ 600px)
- **`data-split-state`** — `"pill"` | `"split"` | `"full"`
- **`data-orientation`** — `"portrait"` | `"side"`

The handle element is injected into `mount`; set `position: relative` on it
and `position: absolute; inset: 0` on both child panes.

**Usage:**

```js
// After window.mobius.chat() resolves:
const split = window.mobius.split({
  mount: document.getElementById('ma-root'),
  defaultRatio: 0.65,   // content takes 65%, chat 35%
  minContentPx: 120,
  minChatPx: 96,
  persistKey: 'split-state-v1',  // sessionStorage key
})
// To programmatically switch state:
split.setState('split')    // 'pill' | 'split' | 'full'
// On unmount:
split.destroy()
```

**JSX mount structure:**

```jsx
<div id="ma-root" className="ma-root ma-root--split">
  <style>{CSS}</style>
  <div data-split-role="content" className="ma-split-content">
    {/* your app content here */}
  </div>
  <div data-split-role="chat" className="ma-split-chat" ref={chatMountRef}>
    {/* window.mobius.chat({ mount: chatMountRef.current, … }) */}
  </div>
  {/* window.mobius.split injects a drag handle here */}
</div>
```

```css
/* mobius-ui:ChatSplit v1 — keep in sync; library candidate. Diverge below the marker only. */
.ma-root--split {
  position: relative;
  overflow: hidden;
}

/* Portrait (stacked): content on top, chat panel below */
.ma-root--split[data-orientation="portrait"] .ma-split-content {
  position: absolute; top: 0; left: 0; right: 0;
  height: var(--cs-content-h, 100%);
  overflow: hidden;
  transition: height 0.18s ease;
}
.ma-root--split[data-orientation="portrait"] .ma-split-chat {
  position: absolute; bottom: 0; left: 0; right: 0;
  top: var(--cs-content-h, 100%);
  overflow: hidden;
  transition: top 0.18s ease;
}

/* Pill state: a fixed-height pill anchor at safe-area bottom */
.ma-root--split[data-split-state="pill"][data-orientation="portrait"] .ma-split-chat {
  top: auto;
  height: 36px;
  background: var(--surface);
  border-top: 1px solid var(--border);
  border-radius: 12px 12px 0 0;
  display: flex; align-items: center; justify-content: center;
}

/* Side-by-side (wide): content left, chat right */
.ma-root--split[data-orientation="side"] .ma-split-content {
  position: absolute; top: 0; left: 0; bottom: 0;
  width: var(--cs-content-w, 65%);
  overflow: hidden;
  transition: width 0.18s ease;
}
.ma-root--split[data-orientation="side"] .ma-split-chat {
  position: absolute; top: 0; right: 0; bottom: 0;
  left: var(--cs-content-w, 65%);
  overflow: hidden;
  transition: left 0.18s ease;
}
/* /mobius-ui:ChatSplit */
```

The `transition` lines are optional but recommended — they give a 180ms ease
when state machine snaps to pill/full. Remove them if you need instant snaps
(e.g. during drag itself, which the helper handles by updating the property
directly without a CSS transition class).
