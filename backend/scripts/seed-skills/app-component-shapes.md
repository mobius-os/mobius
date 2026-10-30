# Mini-app component shapes

Optional structural-pattern catalog for Möbius mini-apps. Read only a matching
section alongside `building-apps-quickstart.md` and `visual-testing.md` when the
brief needs a fixed app shell, modal sheet, tabs, chat, or split pane. Do not
load it for an ordinary root, header, card, or form.

Copy the selected markup + scoped CSS block into the app's `const CSS`, apply a
per-app prefix, keep the kebab role suffix and structure recognizable, and
diverge wherever the app needs to. This is a starting catalog, not a closed
component API.

Copy and diverge freely: each app owns its copy, and there is no shared
library to keep in sync.

## How to use this catalog

The quickstart owns stylesheet, theme-token, accessibility, touch-target,
reduced-motion, and native-dialog rules; do not duplicate them from here.
Copy only the blocks the app actually needs and replace the `ma-` placeholder
with a short app prefix.
Fence comments such as `/* mobius-ui:Card */` are labels, not a sync contract.

The blocks share one quiet default so the app's own composition can lead:

- one continuous background (`--bg`), no tonal header band;
- borderless rows separated by hairlines; a perimeter border, resting shadow
  or accent rail is a deliberate exception that carries meaning;
- generic controls (chevron, close, empty-state mark) use Apps SDK icons:
  `import { ChevronRight, Moon, X } from '@openai/apps-sdk-ui/components/Icon'`;
- coloured text uses a text role and a labelled fill uses a fill role, each
  with today's base colour as its fallback (`var(--accent-text, var(--accent))`,
  `var(--accent-fill, var(--accent))`), so the block reads on any theme;
- every `:hover` sits inside `@media (hover: hover)`, so a tap never leaves a
  stuck hover state on touch.

---

## 1. Root layout — default `Root`, or `AppShell` when you need it

Two shapes. **Default to the lightweight Root**; reach for **AppShell** only when
a fixed header/footer must stay put while a body scrolls under it. Each app OWNS
its copy — fork and comment it; the `mobius-ui:*` fences are labels. No sync
owed.

**Default — `mobius-ui:Root` (lightweight flow).** Content flows, the iframe
scrolls; nothing here can crush or collapse a child.

```jsx
<div className="ma-root">
  <style>{CSS}</style>
  {/* your content; sheets/toasts render last */}
</div>
```

```css
/* mobius-ui:Root — app-owned (no sync owed). */
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
/* mobius-ui:AppShell — app-owned (no sync owed). */
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
/* mobius-ui:Page — app-owned (no sync owed).
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
down. A pinned `ma-header` keeps the continuous full-bleed background while its divider
is inset to the same column (§2); only
the scrolling content gets the column, and a sticky child (a search bar) keeps working
inside the wrapper. On a flow Root the same wrapper caps prose — drop the breakpoint for
pure prose that should never go full-bleed.

**Exempt — do NOT cap.** Immersive / canvas apps that own the whole viewport (a game,
the atlas globe, a map) and true multi-pane editors that manage their own columns
(latex, webstudio, a split editor) are full-bleed by design — a reading column would
letterbox them. `ma-chat-embed` and `ChatSplit` panes size themselves too. If the app is
a single column of content that reads, cap it; if it's a canvas or a multi-pane
workspace, leave it full-bleed.

**Safe area:** pad edge-touching controls with the host-forwarded
`--mobius-safe-top/right/bottom/left` variables, as `building-apps.md`
(Immersive mode) documents. Inside the app frame a direct
`env(safe-area-inset-*)` may read 0, so it is not the portable contract. The
host forwards real values while the app is immersive and zeros while it is
windowed, where the shell chrome already owns the inset, so one rule such as
`padding-bottom: max(24px, var(--mobius-safe-bottom, 0px))` stays compact
windowed and clears the cutout or home indicator immersive. Only the surface
that actually touches an edge needs it: a full-bleed canvas root pads its
controls, a pinned header its top (§2), a bottom sheet its bottom (§3).

---

## 2. Header (`ma-header`) — brand cluster + right-side SLOT

```jsx
<header className="ma-header">
  <div className="ma-brand">
    <span className="ma-mark" aria-hidden="true">{/* app icon, SDK icon, or letter */}</span>
    <div className="ma-brand-text">
      <h1 className="ma-title">Reflection</h1>
      {/* Optional, for live status only — never a tagline or a count the screen already shows. */}
      <span className="ma-subtitle">5 waiting · next run 06:00</span>
    </div>
  </div>
  <div className="ma-header-right">{/* tabs / segmented / badge */}</div>
</header>
```

```css
/* mobius-ui:Header — app-owned (no sync owed).
   PINNING DIFFERS BY ROOT: in AppShell keep "flex: 0 0 auto" (the flex column holds it,
   .ma-scroll scrolls under it); in a flow Root use "position: sticky; top: 0" instead,
   or drop both for a header that scrolls away. */
.ma-header {
  --ma-inset: 16px;          /* the content column's edge; the divider aligns to it */
  position: relative; flex: 0 0 auto;
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  min-height: 48px; padding: 12px var(--ma-inset);
  padding-top: max(12px, var(--mobius-safe-top, 0px));
  background: var(--bg);     /* the shell tone: one continuous background, no band */
}
.ma-header::after {          /* hairline divider inset to the content column, not edge-to-edge */
  content: ""; position: absolute; left: var(--ma-inset); right: var(--ma-inset); bottom: 0;
  border-bottom: 1px solid var(--border);
}
@media (min-width: 760px) {  /* match the mobius-ui:Page cap; drop this if the app has none */
  .ma-header { --ma-inset: max(16px, calc((100% - 680px) / 2)); }
}
.ma-brand { display: flex; align-items: center; gap: 11px; min-width: 0; }
.ma-mark {
  flex: 0 0 auto; width: 30px; height: 30px; border-radius: 9px;  /* 50% circle is an allowed variant */
  display: flex; align-items: center; justify-content: center;
  background: var(--accent-dim);
  color: var(--accent-text, var(--accent)); font-size: 16px; font-weight: 700; line-height: 1;
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
healthy). The mark may be omitted entirely (a title alone is valid). The
subtitle is optional, for live status only: omit it when the only candidate is
a tagline or a count the list below already shows. Keep the header one compact
row (48px minimum); tabs may share that row instead of stacking a second bar
under the shell's own top bar.

**Safe area:** the `padding-top` line above folds the forwarded top inset into
the 12px floor, so a top-pinned header clears the cutout while immersive and
stays compact windowed. Drop it from a header that does not touch the top edge.

---

## 3. Bottom sheet (`ma-sheet`) — the modal dialog

A modal owes four behaviours whatever its shape: focus moves into it and stays
there, Escape and the shell's Back close it, and focus returns to the control
that opened it. A native `<dialog>` opened with `showModal()` provides the
containment, the inert page behind it and the Escape (`cancel`) event; the
effect below adds focus return and a shell Back target.

```jsx
function ConfirmSheet({ open, busy, onCancel, onConfirm, appName }) {
  const ref = useRef(null)
  const cancelRef = useRef(onCancel)
  cancelRef.current = busy ? () => {} : onCancel

  useEffect(() => {
    const dialog = ref.current
    if (!open || !dialog) return
    const opener = document.activeElement
    dialog.showModal()
    // Back closes the sheet (building-apps.md, Back-gesture support).
    let back = window.mobius?.nav?.open('ma-sheet', {
      onBack: () => { back = null; cancelRef.current() },
    })
    return () => {
      back?.close()
      if (dialog.open) dialog.close()
      opener?.focus?.()
    }
  }, [open])

  if (!open) return null
  return (
    <dialog ref={ref} className="ma-sheet" aria-labelledby="ma-sheet-title"
            onCancel={(e) => { e.preventDefault(); cancelRef.current() }}
            onClick={(e) => { if (e.target === e.currentTarget) cancelRef.current() }}>
      <div className="ma-sheet-inner">
        <h3 id="ma-sheet-title" className="ma-sheet-title">Uninstall {appName}?</h3>
        <p className="ma-sheet-body">This removes the app and its stored data.</p>
        {/* optional <input className="ma-input" …/> for a prompt-style sheet */}
        <div className="ma-sheet-actions">
          <button className="ma-btn ma-btn-secondary" onClick={onCancel} disabled={busy}>Cancel</button>
          <button className="ma-btn ma-btn-danger" onClick={onConfirm} disabled={busy}>
            {busy ? 'Removing…' : 'Uninstall'}
          </button>
        </div>
      </div>
    </dialog>
  )
}
```

```css
/* mobius-ui:Sheet — app-owned (no sync owed).
   A modal <dialog> sits in the frame's own top layer, so it never covers shell chrome. */
.ma-sheet {
  box-sizing: border-box; width: 100%; max-width: min(480px, 100%);
  max-height: 85dvh; margin: auto auto 0;   /* flush to the bottom edge */
  padding: 0; border: 0; border-radius: 16px 16px 0 0;
  background: var(--surface); color: var(--text);
  overflow-y: auto; overscroll-behavior: contain;   /* scrolling never chains to the page */
  box-shadow: 0 -8px 32px rgba(0, 0, 0, 0.3);
}
.ma-sheet::backdrop { background: rgba(0, 0, 0, 0.5); }
.ma-sheet-inner { padding: 24px 24px max(24px, var(--mobius-safe-bottom, 0px)); }
.ma-sheet.is-centered { margin: auto; border-radius: 16px; }   /* small confirm variant */
.ma-sheet-title { margin: 0 0 12px; font-size: 16px; font-weight: 700; letter-spacing: -0.01em; }
.ma-sheet-body { margin: 0 0 16px; font-size: 14px; line-height: 1.5; color: var(--muted); }
.ma-sheet-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 24px; }
.ma-sheet-actions .ma-btn { flex: 1; }
/* /mobius-ui:Sheet */
```

The padding lives on the inner wrapper so a click that lands on the dialog
element itself is a backdrop click. Prefer the bottom sheet for pickers and
multi-step touch tasks; `.is-centered` suits a small confirmation. Add a drag
handle only if dragging actually resizes or dismisses the sheet; a handle
with no gesture behind it is misleading. If you keep a hand-built `div` scrim
instead of `<dialog>`, it still owes focus containment, Escape, Back and focus
return.

**Safe area:** the inner bottom padding folds the forwarded bottom inset into
the 24px floor. Any other bottom-pinned surface (a floating pill, a docked
composer) does the same with its own base padding.

---

## 4. Empty state (`ma-empty`) — mark + title + subtitle

```jsx
<div className="ma-empty">
  <div className="ma-empty-mark" aria-hidden="true"><Moon width={28} height={28} /></div>
  <div className="ma-empty-title">No briefs yet</div>
  <p className="ma-empty-text">Reflection runs overnight. Your first morning brief will be waiting here.</p>
</div>
```

```css
/* mobius-ui:Empty — app-owned (no sync owed). */
.ma-empty {  /* AppShell (flex column): flex:1 0 auto fills below the header + centers. Flow Root
                (block — flex is inert): centers within its min-height box, so it sits in the upper
                viewport; bump min-height toward 100dvh for a full-screen header-less empty. */
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  text-align: center; gap: 8px; flex: 1 0 auto; min-height: 60dvh; max-width: 440px;
  margin: 0 auto; padding: 48px 24px; color: var(--muted);
}
.ma-empty-mark {
  width: 64px; height: 64px; margin-bottom: 10px; border-radius: 18px;
  display: flex; align-items: center; justify-content: center;
  background: var(--accent-dim); color: var(--accent-text, var(--accent));
}
.ma-empty-title { font-size: 17px; font-weight: 700; color: var(--text); letter-spacing: -0.01em; }
.ma-empty-text { margin: 0; font-size: 14px; line-height: 1.6; }
/* /mobius-ui:Empty */
```

Every list / feed / graph gets one — never a bare "Nothing here." The mark
tile is optional (drop it for a text-only empty); when present it holds an
Apps SDK icon (`Moon` here) or real domain imagery, never an emoji. Keep the
centered column + the title/text scale.

---

## 5. Card / list row (`ma-card`) — borderless rows with hairlines

```jsx
<div className="ma-list">
  {items.map((d) => (
    <button key={d.id} className={`ma-card${d.id === selectedId ? ' is-selected' : ''}`}
            onClick={() => onOpen(d)}>
      <div className="ma-card-main">
        <div className="ma-card-title">{d.title}</div>
        <div className="ma-card-sub">{d.sub}</div>
      </div>
      <ChevronRight className="ma-card-chevron" width={18} height={18} aria-hidden="true" />
    </button>
  ))}
</div>
```

```css
/* mobius-ui:Card — app-owned (no sync owed). */
.ma-list { display: flex; flex-direction: column; }
.ma-list.is-grouped { background: var(--surface); border-radius: 12px; }   /* optional tonal group, still no border */
.ma-card {
  position: relative; display: flex; align-items: center; gap: 14px; width: 100%; min-height: 44px;
  padding: 14px 16px; text-align: left;
  background: transparent; color: var(--text);
  border: 0; border-radius: 10px; font-family: var(--font);
  transition: background 0.16s ease;
}
.ma-card + .ma-card::before {   /* hairline between rows, inset to the text */
  content: ""; position: absolute; top: 0; left: 16px; right: 16px;
  border-top: 1px solid var(--border);
}
button.ma-card { cursor: pointer; }
@media (hover: hover) {
  button.ma-card:hover { background: var(--surface); }
  .ma-list.is-grouped button.ma-card:hover { background: var(--surface-2); }
}
button.ma-card:focus-visible { outline: 2px solid var(--accent); outline-offset: -2px; }
.ma-card.is-selected { background: var(--accent-dim); }   /* app-driven state = modifier class */
.ma-card-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.ma-card-title { font-size: 15px; font-weight: 600; letter-spacing: -0.01em; }
.ma-card-sub { font-size: 13px; color: var(--muted); }
.ma-card-chevron { flex: 0 0 auto; color: var(--muted); }
/* /mobius-ui:Card */
```

The default is quiet: no perimeter border, no resting shadow, no accent
stripe. Static rows drop the `button` states and the chevron. State
(selected/error) rides `is-`/`has-` modifier classes — never a
`card(variant)` JS helper. A bordered card or a leading accent rail is a
deliberate exception the app adds only where it carries meaning (a decision
rail, an ownership edge), with a comment saying what it marks; never as a
generic "featured" or "latest" decoration.

---

## 6. Buttons (`ma-btn` + `-primary` / `-secondary` / `-ghost` / `-danger` / `-icon`)

```jsx
<button className="ma-btn ma-btn-primary" onClick={save} disabled={busy}>{busy ? 'Saving…' : 'Save'}</button>
<button className="ma-btn ma-btn-secondary" onClick={cancel}>Cancel</button>
<button className="ma-btn ma-btn-ghost" onClick={skip}>Skip</button>
<button className="ma-btn ma-btn-danger" onClick={remove}>Delete</button>
<button className="ma-btn ma-btn-icon" aria-label="Close" onClick={close}>
  <X width={20} height={20} aria-hidden="true" />
</button>
```

```css
/* mobius-ui:Button — app-owned (no sync owed). */
.ma-btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  min-height: 44px; padding: 10px 16px; border-radius: 10px;
  border: 0; background: var(--surface-2, var(--surface)); color: var(--text);
  font-family: var(--font); font-size: 14px; font-weight: 600; cursor: pointer; white-space: nowrap;
  transition: background 0.14s ease, transform 0.1s ease;
}
.ma-btn:active { transform: scale(0.97); }
.ma-btn:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.ma-btn:disabled { opacity: 0.5; cursor: default; transform: none; }
.ma-btn-primary { background: var(--accent-fill, var(--accent)); color: var(--accent-fg); }
.ma-btn-secondary { background: var(--surface-2, var(--surface)); }
.ma-btn-ghost { background: transparent; color: var(--accent-text, var(--accent)); }
.ma-btn-danger { background: var(--danger-fill, var(--danger)); color: var(--accent-fg); }
.ma-btn-success { background: var(--green-fill, var(--green)); color: var(--accent-fg); }
.ma-btn-icon { width: 44px; padding: 0; border-radius: 8px; background: transparent; color: var(--muted); }   /* icon-only → needs aria-label */
@media (hover: hover) {
  .ma-btn-primary:hover:not(:disabled) { background: var(--accent-fill-hover, var(--accent-hover)); }
  .ma-btn-secondary:hover:not(:disabled) { background: color-mix(in srgb, var(--text) 8%, var(--surface-2, var(--surface))); }
  .ma-btn-ghost:hover:not(:disabled), .ma-btn-icon:hover:not(:disabled) { background: var(--accent-dim); }
}
/* /mobius-ui:Button */
```

The highest-value shared shape. A labelled fill uses a fill role —
`--accent-fill`, `--danger-fill` or `--green-fill` — with `--accent-fg` as its
label, and ghost or link text uses `--accent-text`. These roles keep labels and
coloured text readable in both modes; each example falls back to the base
colour (`--accent`, `--danger`, `--green`) on a theme that does not define the
role. Outside that fallback, never put `--accent-fg` on a base colour or use a
base colour as small text. Theme the pair once at the token, not per app.
Where a view has one clear main task, give that action the filled primary and
keep repeated row actions tonal or ghost.
A full-width form-submit adds `width: 100%` via a `.ma-btn-block` modifier.

---

## 7. Input / textarea (`ma-input` / `ma-textarea`)

```jsx
<input className="ma-input" type="search" inputMode="search" enterKeyHint="search"
       autoComplete="off" value={v} onChange={(e) => set(e.target.value)} placeholder="Search" />
<textarea className="ma-textarea" value={t} onChange={(e) => set(e.target.value)} />
```

Match `type`, `inputMode`, `enterKeyHint` and `autoComplete` to what each
field means (the quickstart's input-semantics line); the search values above
are an example, not a default.

```css
/* mobius-ui:Input — app-owned (no sync owed). */
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
/* mobius-ui:Segmented — app-owned (no sync owed). */
.ma-seg {
  display: inline-flex; gap: 2px; height: 44px;
  background: var(--surface-2, var(--surface)); border: 0; border-radius: 10px;
  box-shadow: inset 0 0 0 1px var(--border);
}
.ma-seg-btn {
  box-sizing: border-box; min-height: 44px; padding: 6px 14px; border: 0; border-radius: 7px;
  background: transparent; color: var(--muted); font-family: var(--font);
  font-size: 13px; font-weight: 650; cursor: pointer; transition: background 0.15s, color 0.15s;
}
@media (hover: hover) { .ma-seg-btn:hover { color: var(--text); } }
.ma-seg-btn.is-active { background: var(--bg); color: var(--text); box-shadow: 0 1px 3px rgba(0, 0, 0, 0.18); }
.ma-seg.is-tinted .ma-seg-btn.is-active { background: var(--accent-dim); color: var(--accent-text, var(--accent)); box-shadow: none; }
/* /mobius-ui:Segmented */
```

Default active is the subtle raised chip (theme-safe on any accent); opt into
`.ma-seg.is-tinted` for a tonal accent selection. Selection is a tone, action
is a fill: never paint the selected segment with the primary button's
`--accent-fill`, or the current view reads as a button to press. Add
`role="tablist"` + `aria-selected` when it switches views; `flex: 1` per
button for a full-width tab bar.

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
/* mobius-ui:ImageRail — app-owned (no sync owed). */
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
/* mobius-ui:ChatEmbed — app-owned (no sync owed). */
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
/* mobius-ui:Focus — app-owned (no sync owed). Required once per app. */
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

/* mobius-ui:ReducedMotion — app-owned (no sync owed). Required once per app. */
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 0.01ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 0.01ms !important;
    scroll-behavior: auto !important;
  }
}
/* /mobius-ui:ReducedMotion */

/* mobius-ui:Spinner — app-owned (no sync owed). */
@keyframes ma-spin { to { transform: rotate(360deg); } }
.ma-spinner {
  width: 26px; height: 26px; border-radius: 50%;
  border: 2.5px solid color-mix(in srgb, var(--accent) 18%, transparent); border-top-color: var(--accent);
  animation: ma-spin 0.8s linear infinite;
}
@media (prefers-reduced-motion: reduce) { .ma-spinner { animation: none; } }   /* mandatory */
/* /mobius-ui:Spinner */

/* mobius-ui:Toast — app-owned (no sync owed). */
.ma-toast {
  position: absolute; left: 16px; right: 16px; bottom: 16px; z-index: 200;   /* absolute → inside the app */
  display: flex; align-items: center; gap: 12px; padding: 12px 16px;
  background: var(--surface); border: 1px solid var(--accent); border-radius: 12px;
  font-size: 14px; line-height: 1.5; box-shadow: 0 8px 24px rgba(0,0,0,0.4);
}
.ma-toast.is-success { border-color: var(--green); }
.ma-toast.is-error { border-color: var(--danger); }
/* /mobius-ui:Toast */

/* mobius-ui:Disclosure — app-owned (no sync owed).
   A <details>/<summary> accordion item: <summary> IS the control (it carries the
   44px tap-target + focus-ring, not a child button). This is the flex-crush-safe
   accordion the AppShell crush note points to — a native <details> needs no
   flex-shrink workaround in flow, and gets flex-shrink:0 inside .ma-scroll. */
.ma-disc + .ma-disc { border-top: 1px solid var(--border); }   /* hairline rows, like Card */
.ma-disc > summary { list-style: none; cursor: pointer; min-height: 44px; display: flex;
  align-items: center; gap: 12px; padding: 12px 16px; font-size: 15px; font-weight: 650; color: var(--text); }
.ma-disc > summary::-webkit-details-marker { display: none; }
.ma-disc-body { padding: 0 16px 14px; font-size: 14px; line-height: 1.6; color: var(--muted); }
/* /mobius-ui:Disclosure */

/* mobius-ui:SectionHead — app-owned (no sync owed). */
.ma-section-head { display: flex; align-items: center; gap: 10px; margin: 0 0 8px; }
.ma-section-icon {
  width: 30px; height: 30px; flex: 0 0 auto; border-radius: 9px;
  display: flex; align-items: center; justify-content: center;
  background: var(--accent-dim); color: var(--accent-text, var(--accent));   /* holds an SDK icon at 16px */
}
.ma-section-label { margin: 0; font-size: 14.5px; font-weight: 700; letter-spacing: -0.01em; }
/* /mobius-ui:SectionHead */

/* mobius-ui:Scrollskin — app-owned (no sync owed). Add the ma-scroll class to a scroller. */
.ma-scroll::-webkit-scrollbar { width: 9px; height: 9px; }
.ma-scroll::-webkit-scrollbar-thumb { background: var(--border); border-radius: 999px; border: 2px solid transparent; background-clip: padding-box; }
@media (hover: hover) { .ma-scroll::-webkit-scrollbar-thumb:hover { background: var(--muted); background-clip: padding-box; } }
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
/* mobius-ui:SyncPill — app-owned (no sync owed). SILENT WHEN HEALTHY:
   not mounted while online (never "Saving" / pending counts); plain "Offline"
   when offline; .is-error only for a failure the owner can act on. */
.ma-sync-pill {
  position: absolute; right: 12px; bottom: max(12px, var(--mobius-safe-bottom, 0px)); z-index: 40;
  display: inline-flex; align-items: center; padding: 6px 12px; border-radius: 999px;
  background: var(--surface); border: 1px solid var(--border); color: var(--muted);
  font-size: 12px; font-weight: 600; box-shadow: 0 2px 8px rgba(0,0,0,0.18);
}
.ma-sync-pill.is-error { border-color: var(--danger); color: var(--danger-text, var(--danger)); }
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
/* mobius-ui:ChatSplit — app-owned (no sync owed). */
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
