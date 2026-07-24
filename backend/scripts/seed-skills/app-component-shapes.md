# Mini-app component shapes

Optional UI-pattern catalog for Möbius mini-apps. Read only the relevant
section, alongside `building-apps-quickstart.md` and `visual-testing.md`, when
the app needs a recurring root, header, sheet, empty, card, form, tab, chat, or
split-pane structure.

Copy the selected markup + scoped CSS block into the app's `const CSS`, apply a
per-app prefix, keep the kebab role suffix and structure recognizable, and
diverge wherever the app needs to. This is a starting catalog, not a closed
component API.

Why copies and not a shared library yet: a shared component freezes an API
before the shapes have proven stable, and then any app that needs something it
didn't anticipate hits a wall. Copies let each app diverge its own CSS freely —
full CSS power, no permission, no blast radius. When ~3 apps carry the same
fenced block (same role + structure, just a different prefix), it has earned
extraction into a real `@mobius/ui` you import — a `grep` of the fence names
finds the kin. Until then, owning your fork is correct. This is the platform's
"code empowers the agent; it does not police it," in CSS form.

## How to use this catalog

The quickstart owns stylesheet, theme-token, accessibility, touch-target,
reduced-motion, and native-dialog rules; do not duplicate them from here.
Copy only the blocks the app actually needs and replace the `ma-` placeholder
with a short app prefix.

Fence comments such as `/* mobius-ui:Card */` are harvest markers, not a sync
contract. They make similar app-owned copies discoverable if a pattern later
earns extraction into a real shared library. Until then, the app owns and may
diverge its copy.

---

## 1. Root layout — default `Root`, or `AppShell` when you need it

Two shapes. **Default to the lightweight Root**; reach for **AppShell** only when
a fixed header/footer must stay put while a body scrolls under it. Each app OWNS
its copy — fork and comment it; the `mobius-ui:*` fences only mark blocks a future
shared library could be harvested from. No sync owed.

**Default — `mobius-ui:Root` (lightweight flow).** Content flows, the iframe
scrolls; nothing here can crush or collapse a child.

```jsx
<div className="ma-root">
  <style>{CSS}</style>
  {/* your content; sheets/toasts render last */}
</div>
```

```css
/* mobius-ui:Root — app-owned; a future-library candidate (no sync owed). */
.ma-root {
  box-sizing: border-box;
  position: relative;        /* anchor for absolute scrims / sheets / toasts */
  min-height: 100dvh;
  overflow-x: clip;          /* clip, NOT hidden: stops x-bleed without making the root a
                                scroll container, which would break a position:sticky header */
  background: var(--bg); color: var(--text); font-family: var(--font);
  -webkit-font-smoothing: antialiased;
}
/* /mobius-ui:Root */
```

Pin a header with `position: sticky; top: 0` (omit it for a header that scrolls
away). For a reading column (prose, a changelog, an FAQ) cap an inner wrapper with
the `mobius-ui:Page` block below; for an FAQ/accordion add the `mobius-ui:Disclosure`
block (§10) — a native `<details>` flows fine in a Root, no AppShell needed. The
`.ma-empty` block centers per its own note.

**Opt-in — `mobius-ui:AppShell` (pinned header + independent scroll).** For lists
/ feeds / a fixed input bar: a flex column whose body scrolls under a fixed header.

```jsx
<div className="ma-root">
  <style>{CSS}</style>
  <header className="ma-header">…</header>
  <div className="ma-scroll">…</div>
  {/* sheets/toasts render here, last */}
</div>
```

```css
/* mobius-ui:AppShell — app-owned; a future-library candidate (no sync owed). */
.ma-root {
  position: relative;        /* anchor for scrims / sheets / toasts (absolute, not fixed) */
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
.ma-scroll > * { flex-shrink: 0; }  /* keep children at natural height — without this a
                                       child with small min-content (details/summary/img/
                                       canvas) is crushed by flex-shrink */
/* /mobius-ui:AppShell */
```

Diverge on padding, a desktop `max-width` cap (the `mobius-ui:Page` block below), or
(rarely, for a full-bleed canvas like a map) `position: fixed; inset: 0`. On AppShell,
`min-height: 0` on the scroll child is non-negotiable.

**Reading column — `mobius-ui:Page` (cap CONTENT on wide viewports).** A mobile-first
app looks right at 412px and then, at 1280px, its rows / cards / prose stretch
edge-to-edge into ~180-char lines. The fix is a reading column: the scroll container
stays full-bleed (its scrollbar hugs the device edge), and an inner `.ma-page` wrapper
caps the CONTENT — centered above a ~760px breakpoint, full-bleed below it so phones are
untouched. Cap the CONTENT wrapper, never the scroll container itself — a `max-width` on
the scroller strands its scrollbar in mid-screen.

```jsx
<div className="ma-scroll">          {/* full-bleed; owns the scroll + scrollbar */}
  <div className="ma-page">          {/* capped, centered reading column */}
    {/* search, list, cards, empty — the scrolling content */}
  </div>
</div>
```

```css
/* mobius-ui:Page — app-owned; a future-library candidate (no sync owed).
   Reading column: full-bleed on phones, centered + capped on wide viewports.
   Cap the CONTENT wrapper, never the scroll container (a capped scroller floats
   its scrollbar mid-screen). */
.ma-page { width: 100%; }
@media (min-width: 760px) {
  .ma-page { max-width: 680px; margin-inline: auto; }
}
/* /mobius-ui:Page */
```

Pick the cap to match the app's own content: 680px reads well for text; a card list
that already caps its detail / reader view (640–720px) should reuse THAT number so list
and detail agree — mismatched caps are the same edge-to-edge complaint one zoom level
down. A pinned `ma-header` stays full-bleed (its divider still reaches both edges); only
the scrolling content gets the column, and a sticky child (a search bar) keeps working
inside the wrapper. On a flow Root the same wrapper caps prose — drop the breakpoint for
pure prose that should never go full-bleed.

**Exempt — do NOT cap.** Immersive / canvas apps that own the whole viewport (a game,
the atlas globe, a map) and true multi-pane editors that manage their own columns
(latex, webstudio, a split editor) are full-bleed by design — a reading column would
letterbox them. `ma-chat-embed` and `ChatSplit` panes size themselves too. If the app is
a single column of content that reads, cap it; if it's a canvas or a multi-pane
workspace, leave it full-bleed.

**Safe area:** a full-bleed root (one that paints to the device edges — a
`position: fixed; inset: 0` canvas, or a root with no edge-pinned header/sheet
chrome of its own) must inset its content for the notch and home indicator:
`padding: env(safe-area-inset-top) env(safe-area-inset-right)
env(safe-area-inset-bottom) env(safe-area-inset-left)`. A normal root whose
header and bottom sheet already handle their own edges (below) does NOT need
this — only add safe-area on the surface that actually touches an edge.
`env(safe-area-inset-*)` resolves correctly both in-shell and standalone (the
iframe ships `viewport-fit=cover`). For an app that goes **immersive** (hides
shell chrome — see building-apps.md), the background already bleeds full-screen;
pad controls with `env()` or the shell's `--mobius-safe-*` vars (the latter are
0 while windowed, so use them when you want inset padding only while immersive).

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
  <div className="ma-header-right">{/* tabs / segmented / badge */}</div>
</header>
```

```css
/* mobius-ui:Header — app-owned; a future-library candidate (no sync owed).
   PINNING DIFFERS BY ROOT: in AppShell keep "flex: 0 0 auto" (the flex column holds it,
   .ma-scroll scrolls under it); in a flow Root use "position: sticky; top: 0" instead,
   or drop both for a header that scrolls away. */
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

The right side is a SLOT — drop your tabs / toggle / badge in. Never a sync
or save-status indicator here (see the SyncPill rule in §10 — silent when
healthy). The mark may be omitted entirely (title + subtitle alone is valid).
Don't diverge on the flex/space-between skeleton or the 48px min-height.

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
/* mobius-ui:Sheet — app-owned; a future-library candidate (no sync owed). */
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
  <p className="ma-empty-text">Reflection runs overnight. Your first morning brief will be waiting here.</p>
</div>
```

```css
/* mobius-ui:Empty — app-owned; a future-library candidate (no sync owed). */
.ma-empty {  /* AppShell (flex column): flex:1 0 auto fills below the header + centers. Flow Root
                (block — flex is inert): centers within its min-height box, so it sits in the upper
                viewport; bump min-height toward 100dvh for a full-screen header-less empty. */
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  text-align: center; gap: 8px; flex: 1 0 auto; min-height: 60dvh; max-width: 440px;
  margin: 0 auto; padding: 48px 24px; color: var(--muted);
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
/* mobius-ui:Card — app-owned; a future-library candidate (no sync owed). */
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
/* mobius-ui:Button — app-owned; a future-library candidate (no sync owed). */
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
/* mobius-ui:Input — app-owned; a future-library candidate (no sync owed). */
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
/* mobius-ui:Segmented — app-owned; a future-library candidate (no sync owed). */
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

### Image rail (`mobius-ui:ImageRail`)

For two or more related photos, prefer a compact filmstrip over a wrapping
thumbnail grid. The narrow layout deliberately shows part of the next image so
the horizontal gesture is discoverable; wide layouts settle into a quiet
three-up row. A tap should open the original image in the app's focused viewer.

Keep touch and trackpad scrolling NATIVE. Do not translate pointer movement
into `scrollLeft`: that removes mobile momentum and competes with the app's
vertical scroll. If desktop mouse users need an alternative, add 44px
previous/next buttons that call `scrollBy({ behavior: "smooth" })`; keep Arrow
keys on the focusable rail as the keyboard equivalent.

```jsx
<section className="ma-image-gallery" aria-label={`Related images, ${images.length} items`}>
  <div
    className="ma-image-rail"
    role="group"
    aria-label="Related images. Swipe or use arrow keys to browse."
    tabIndex={0}
  >
    {images.map((image) => (
      <button
        type="button"
        className="ma-image-tile"
        aria-label={`Open ${image.alt || "image"} preview`}
        onClick={() => openImage(image)}
        key={image.src}
      >
        <img src={image.src} alt={image.alt || ""} loading="lazy" />
      </button>
    ))}
  </div>
</section>
```

```css
/* mobius-ui:ImageRail — app-owned; a future-library candidate (no sync owed). */
.ma-image-gallery { container-type: inline-size; min-width: 0; }
.ma-image-rail {
  display: grid; grid-auto-flow: column; grid-auto-columns: 78cqi; gap: 4px;
  min-width: 0; overflow-x: auto; overscroll-behavior-x: contain;
  scroll-snap-type: x proximity; scroll-padding-inline: 4px;
  scrollbar-width: none; -webkit-overflow-scrolling: touch;
  touch-action: pan-x pan-y; padding: 0 0 4px;
}
.ma-image-rail::-webkit-scrollbar { display: none; }
.ma-image-rail:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; border-radius: 12px; }
.ma-image-tile {
  min-height: 44px; min-width: 0; aspect-ratio: 4 / 3; padding: 0;
  overflow: hidden; scroll-snap-align: start; border: 0; border-radius: 10px;
  background: var(--surface); cursor: pointer;
}
.ma-image-tile img { display: block; width: 100%; height: 100%; object-fit: cover; }
@container (min-width: 30rem) {
  .ma-image-rail { grid-auto-columns: calc((100cqi - 8px) / 3); }
}
/* /mobius-ui:ImageRail */
```

---

## 9. Agent-chat mount (`ma-chat-embed`)

The wrapper that holds `window.mobius.chat(...)`. The load-bearing rule:
`min-height: 0` (flex panel) or an explicit height (box), `overflow: hidden`,
and the iframe fills it. See the [building-apps.md] "Agent-powered mini-apps"
section for the one-call helper (`persist` + `onTurnDone`).

```css
/* mobius-ui:ChatEmbed — app-owned; a future-library candidate (no sync owed). */
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
/* mobius-ui:Focus — app-owned; a future-library candidate (no sync owed). Required once per app. */
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

/* mobius-ui:ReducedMotion — app-owned; a future-library candidate (no sync owed). Required once per app. */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
/* /mobius-ui:ReducedMotion */

/* mobius-ui:Spinner — app-owned; a future-library candidate (no sync owed). */
@keyframes ma-spin { to { transform: rotate(360deg); } }
.ma-spinner {
  width: 26px; height: 26px; border-radius: 50%;
  border: 2.5px solid color-mix(in srgb, var(--accent) 18%, transparent); border-top-color: var(--accent);
  animation: ma-spin 0.8s linear infinite;
}
@media (prefers-reduced-motion: reduce) { .ma-spinner { animation: none; } }   /* mandatory */
/* /mobius-ui:Spinner */

/* mobius-ui:Toast — app-owned; a future-library candidate (no sync owed). */
.ma-toast {
  position: absolute; left: 16px; right: 16px; bottom: 16px; z-index: 200;   /* absolute → inside the app */
  display: flex; align-items: center; gap: 12px; padding: 12px 16px;
  background: var(--surface); border: 1px solid var(--accent); border-radius: 12px;
  font-size: 14px; line-height: 1.5; box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}
.ma-toast.is-success { border-color: var(--green); }
.ma-toast.is-error { border-color: var(--danger); }
/* /mobius-ui:Toast */

/* mobius-ui:Disclosure — app-owned; a future-library candidate (no sync owed).
   A <details>/<summary> accordion item: <summary> IS the control (it carries the
   44px tap-target + focus-ring, not a child button). This is the flex-crush-safe
   accordion the AppShell crush note points to — a native <details> needs no
   flex-shrink workaround in flow, and gets flex-shrink:0 inside .ma-scroll. */
.ma-disc { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
.ma-disc[open] { border-color: color-mix(in srgb, var(--accent) 45%, var(--border)); }
.ma-disc > summary { list-style: none; cursor: pointer; min-height: 44px; display: flex;
  align-items: center; gap: 12px; padding: 12px 16px; font-size: 15px; font-weight: 650; color: var(--text); }
.ma-disc > summary::-webkit-details-marker { display: none; }
.ma-disc-body { padding: 0 16px 14px; font-size: 14px; line-height: 1.6; color: var(--muted); }
/* /mobius-ui:Disclosure */

/* mobius-ui:SectionHead — app-owned; a future-library candidate (no sync owed). */
.ma-section-head { display: flex; align-items: center; gap: 10px; margin: 0 0 8px; }
.ma-section-icon {
  width: 30px; height: 30px; flex: 0 0 auto; border-radius: 9px;
  display: flex; align-items: center; justify-content: center;
  background: color-mix(in srgb, var(--accent) 14%, transparent); font-size: 15px;
}
.ma-section-label { margin: 0; font-size: 14.5px; font-weight: 700; letter-spacing: -0.01em; }
/* /mobius-ui:SectionHead */

/* mobius-ui:Scrollskin — app-owned; a future-library candidate (no sync owed). Add the ma-scroll class to a scroller. */
.ma-scroll::-webkit-scrollbar { width: 9px; height: 9px; }
.ma-scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 999px; border: 2px solid transparent; background-clip: padding-box; }
.ma-scroll::-webkit-scrollbar-thumb:hover { background: var(--muted); background-clip: padding-box; }
.ma-scroll::-webkit-scrollbar-track { background: transparent; }
/* /mobius-ui:Scrollskin */
```

### SyncPill — SILENT WHEN HEALTHY

Render NOTHING while online. Saving and pending writes are invisible plumbing
— `window.mobius.storage` queues writes safely — not information; never show
"Saving…", pending-write counters, or last-synced timestamps while online.
Mount the pill ONLY when the app is offline, with the plain text "Offline" (no
counts, no timestamps). The one other state that may surface is an
error/conflict the owner must act on (`.is-error`), plainly worded
("Couldn't save — tap to retry").

```jsx
{/* track `online` via window.mobius.online + the window 'online'/'offline' events */}
{!online && <div className="ma-sync-pill" role="status">Offline</div>}
```

```css
/* mobius-ui:SyncPill — app-owned; a future-library candidate (no sync owed). SILENT WHEN HEALTHY:
   not mounted while online (never "Saving" / pending counts); plain "Offline"
   when offline; .is-error only for a failure the owner can act on. */
.ma-sync-pill {
  position: absolute; right: 12px; bottom: 12px; z-index: 40;
  display: inline-flex; align-items: center; padding: 6px 12px; border-radius: 999px;
  background: var(--surface); border: 1px solid var(--border); color: var(--muted);
  font-size: 11px; font-weight: 600; box-shadow: 0 2px 8px rgba(0,0,0,0.18);
}
.ma-sync-pill.is-error { border-color: var(--danger); color: var(--danger); }
/* /mobius-ui:SyncPill */
```

No FAB shape is specced — zero apps use a floating action button today.
Compose affordances live inline in a header or list. If a future app needs
one, build it from the `.ma-btn-primary` look + the sync-pill's floating
mechanics, with an `aria-label`.

---

## 11. ChatSplit — embedded chat panel with pill ↔ split ↔ full state machine

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
/* mobius-ui:ChatSplit — app-owned; a future-library candidate (no sync owed). */
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
