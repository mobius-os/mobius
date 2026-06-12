# Building mini-apps

The full mini-app contract: component shape, `window.mobius.storage` and its traps, the app lifecycle (register-on-create only), offline, fetching, embedded app chats, back-navigation, and theming. `Read` this before building or updating any mini-app.

Mini-apps are JSX components in sandboxed iframes. Each gets `appId` and `token` props and persists through `window.mobius.storage`. The iframe is same-origin, so all browser storage works and `fetch('/api/...')` is free; this also means a mini-app can read the owner JWT — an accepted single-owner trade-off, not a license to be careless.

---

## Before building: check existing apps

Default to checking what already exists before creating a new one:

```bash
curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/apps/" | python3 -m json.tool
```

If an app with the same purpose exists, update it instead of duplicating. If the partner says "build X" and X already exists, confirm whether they want to update or replace it.

---

## Packaging / wrapping a pre-built or third-party web app

Sometimes the app you want isn't authored fresh in JSX — it's an existing built web app (a React/Vite/CRA/WebGL game, a tool's `dist/`) that you mount whole. The durable pattern is a thin Möbius wrapper (a small `index.jsx` around an iframe) plus a `mobius.json` that declares the build's own files as `static_assets`, so Möbius serves them under the app's asset route. Do **not** copy built files into `/data/shell` or `/data/shell/dist`: deploy refreshes replace that tree, so the app disappears or, worse, `/some-app/index.html` falls through to the Möbius shell and opens Möbius inside Möbius.

Mechanics:

1. Clone the upstream third-party repo as input.
2. Make the app build as a static site with **relative** asset paths whenever the framework supports it (`PUBLIC_URL=.`, `homepage: "."`, Vite `base: "./"`, etc.).
3. Build into `build/` or `dist/`.
4. From the repo root, run:

```bash
node /app/scripts/package-static-app.mjs \
  --id cuberun \
  --name "CubeRun" \
  --version "1.0.0-mobius.1" \
  --description "Neon 3D runner game packaged for Mobius." \
  --homepage "https://github.com/<you>/<your-app>" \
  --build-dir build \
  --out-dir . \
  --icon icon.png
```

The packager writes `mobius.json` and an iframe `index.jsx`, enumerates every build file as `static_assets`, rewrites root-relative HTML/CSS asset references such as `/static/js/main.js` or `/fonts/foo.ttf` when the target exists inside the build, and fails on unresolved local CSS URLs. Re-run with `--force` after edits. Verify the generated package before installing:

```bash
node /app/scripts/package-static-app.mjs --help
npm run test:packager --prefix /home/hmzmrzx/projects/mobius
```

Install it through the app installer (it registers the local package — no public repo or GitHub push), not by hand-copying into `/data`:

```bash
curl -s -X POST "$API_BASE_URL/api/apps/install" \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"manifest_url":"<url-to-your-mobius.json>"}'
```

Runtime smoke checks for this class of app:

- `/app-assets/by-id/<app-id>/index.html` returns the actual static app HTML.
- `/app-assets/by-id/<app-id>/static/...` and fonts/media return 200.
- Old ad-hoc routes such as `/cuberun/index.html` return 404, not the shell HTML.
- Opening `/shell/?app=<app-id>` shows the game/tool inside the nested iframe, not a copy of Möbius.

If a package update leaves `.mobius-bak` files or a dirty `/data/apps/<slug>` git tree, that is installer noise, not app source; re-run the installer on a backend that includes the static-asset backup fix.

### What to watch for

The wrapper is a thin Möbius app around someone else's build. The gotchas, learned the hard way adapting one:

1. **Shape: a thin `index.jsx` wrapper around an iframe.** The wrapper mounts the build's entry HTML in an `<iframe>` and stays small — chrome (loader, error state) plus the iframe, nothing more. The build's own files are declared in `mobius.json` `static_assets` as a map of *logical path → build file* so Möbius serves them under the app's asset route. Set `offline_capable: false` unless EVERY asset the build pulls is precached (an offline-capable wrapper that fetches one uncached chunk renders broken).

2. **Relative asset paths are mandatory.** Build with a relative public base — CRA `homepage: "."` (`PUBLIC_URL=.`), Vite `base: './'`. Absolute references like `/static/js/main.js` 404 under the app serve path (the app lives at `/app-assets/by-id/<id>/`, not site root), so the build loads blank. Rebuild with the relative base and re-check the emitted HTML/CSS reference assets relatively.

3. **No external CDNs — prod CSP is `default-src 'self'`.** The only off-origin source allowed is `esm.sh` for importmap scripts; everything else (a webfont from Google Fonts, a wasm/decoder fetched from a CDN, analytics, an external `<img>`) is silently blocked, so the build half-works with no console clue in some cases. **Grep the build for `https://`** before mounting; for each hit, vendor the asset same-origin (add it to `static_assets`) or route it through `/api/proxy`. If a library defaults to fetching something from a CDN, disable that default when the asset isn't actually needed.

4. **Probe the entry asset before mounting.** HEAD/GET the build's entry HTML (or its main JS) first; if it's missing, render a clean, actionable error (Retry / Reinstall) inside the themed wrapper rather than leaving a blank iframe. A blank frame reads as "the whole platform broke"; a labeled error tells the owner exactly what to do.

5. **Theme the wrapper chrome.** The loader and error states use `var(--bg) / var(--surface) / var(--text) / var(--border) / var(--accent)` and `var(--font)`, and honor `prefers-reduced-motion` on any spinner — so the chrome tracks the owner's theme instead of clashing with the embedded build.

6. **Keep `static_assets` consistent with the build, and validate it.** Hashed filenames (`main.4f2a.js`) change on every rebuild, so a manifest written against an old build points at files that no longer exist — a stale manifest is 404s is a broken app. Run a package validator that confirms every `static_assets` entry resolves to a real file in the build before installing, and re-generate the manifest whenever you rebuild.

7. **Fix forward, no dead references.** If a build ships a feature you don't use that pulls an external/CSP-blocked resource (e.g. a compression decoder for an asset you actually ship uncompressed), disable that feature outright rather than leaving the dead CDN reference in place "just in case." A dead reference is either a silent CSP failure or future confusion; remove it.

8. **Mark invented business details as PLACEHOLDERS.** When localizing or rebranding a site (a garage, a shop, a clinic), any address, phone number, price, or testimonial you didn't get from the owner is fabricated — flag it as a placeholder the owner must replace (an inline `<!-- PLACEHOLDER: real address -->` plus a line in your handoff), don't present an invented Sarajevo address and phone as finished contact facts. Made-up contact info reads as done and ships a lie.

(This is the technical packaging/wrapping pattern only. Mounting and serving the build inside this instance is the whole job — there is no public-repo publish step here.)

---

## Component shape

```jsx
export default function MyApp({ appId, token }) {
  return <div>...</div>
}
```

---

## Lifecycle — source folder, register on create, just save to edit

1. Create a source folder at `/data/apps/<name>/`.
2. Put the app entrypoint at `/data/apps/<name>/index.jsx`.
3. Split larger apps into sibling `.js`, `.jsx`, `.ts`, or `.tsx` modules inside that same folder and import them relatively from `index.jsx`.
4. Keep durable static build assets under `static/`; keep runtime data in storage, not in the source folder.

Example:

```text
/data/apps/mood-board/
  index.jsx
  Board.jsx
  cards.js
  static/
    sample.png
```

On first create only, register + compile (mints the id + DB row):

```bash
python "$SCRIPTS_DIR/register_app.py" "<name>" "<description>" /data/apps/<name>/index.jsx
```

`register_app.py` reads `$CHAT_ID` from the environment and stores it with the app so crash reports route back to this chat.

**For edits, just write source files — do NOT re-run `register_app.py`.** A file watcher recompiles when `index.jsx` or a source-like sibling module changes under `/data/apps/<slug>/` (ignoring generated/static dirs such as `static/`, `.build/`, `dist/`, `node_modules/`, and `.git/`). Re-running the script creates a DUPLICATE every time the name differs by a character (slug-vs-title is the common slip). If the partner says it didn't change, check that `/data/compiled/app-<id>.js` mtime advanced and look for `compile failed for` in `/data/logs/chat.log` — a JSX syntax error or broken import blocks the recompile. If a duplicate appears, `DELETE /api/apps/<dup-id>`.

**Use `register_app.py`, not raw `curl POST /api/apps/`.** The raw endpoint requires an undocumented `jsx_source` field (422 without it); updates are `PATCH` not `PUT` (405). The helper handles all of this — skipping it burns tool calls rediscovering the schema from error responses.

### Verify your own output — don't make the owner the test loop

Confirm the change works before handing control back, especially for a bug the owner already reported once: bouncing the same fix back unverified ("hit Build/preview and tell me if it works") is the failure mode, and it compounds when you claim "fixed" twice without ever checking. You can't drive the live shell UI yourself — it needs the owner's password — so verify by the strongest available proxy and SAY which one you used and where it stopped: byte-check the served code (`curl` the compiled module / static asset and grep for the fix), walk the full dependency chain over HTTP (each import/asset returns 200, not the SPA HTML fallback), and curl the actual `/api/...` path end-to-end so a broken link or no-op handler shows up before the owner finds it. Name the verification ceiling you hit ("compiled module carries the fix and `/api/storage/...` round-trips; I can't drive the live tap myself, so confirm the anchor scrolls on your end") instead of ending every turn by punting the test to the owner.

### Don't fabricate — clarify, then cite or hedge

When the owner asks you to "figure out something based on my preferences," ASK the clarifying questions and WAIT for the answers before producing a "tailored" result; never assert constraints they never gave (a cuisine, a budget, an occasion). For date-sensitive or live facts — fixtures, venues, standings, prices — fetch and cite (via `/api/proxy`) or hedge explicitly; when the facts aren't determined yet, give the structural answer and name the unknowns rather than inventing specifics that read as settled.

### Module hierarchy — split a growing app on concept boundaries

A trivial app is one `index.jsx`. As it grows, split on **concept boundaries,
not line count** — when a file is doing two unrelated jobs (you'd describe it
with an "and": "it holds the storage layer AND the globe AND the modal"), lift
each job into a sibling module. (~200 lines is only a smell that prompts the
look, never the trigger itself.) A clean hierarchy keeps each file small enough
to reason about and edit reliably, and it keeps the shared chrome quarantined
so a future library extraction is mechanical.

Canonical layout for a complex app (a one-way dependency graph — nothing
imports `index.jsx`):

```text
/data/apps/<slug>/
  index.jsx        # the default-export App shell + composition ONLY (stays thin)
  storage.js       # the window.mobius.storage data layer: typed get/set per record + the schema
  domain.js        # pure logic / helpers (no React, no I/O) — the testable core
  theme.js         # export const CSS = `...`  (split the stylesheet here once it's large)
  chat.js          # the window.mobius.chat integration, if the app has one
  ui/
    Chrome.jsx     # the shared `mobius-ui:` chrome (Header, Sheet, EmptyState, …) — all fenced
    <Feature>.jsx  # one file per view / feature
  static/          # durable build assets (ignored by the watcher, not importable)
```

- **Keep the literal `export default` in `index.jsx`** — the compiler entry
  point. Siblings import relatively (`./storage.js`, `./ui/Chrome.jsx`) and the
  watcher recompiles when any source sibling changes.
- **Split the stylesheet as a `.js` exporting a CSS string** (`export const
  CSS = \`...\``), NOT a sibling `.css` import — esbuild emits a `.css` import
  as a separate artifact the single-module serving path won't deliver, so the
  app loads unstyled. A `.js` CSS string is just JS and serves fine.
- **Put every `mobius-ui:` fenced block in one `ui/Chrome.jsx`** so the shared,
  library-candidate components are in one place per app. When ~3 apps carry the
  same fenced block, extraction is `grep -rl 'mobius-ui:'` + move + import — the
  copy-then-extract discipline, applied within and across apps.
- Don't pre-abstract. Colocate first; split when a real second concept appears.
  An over-split trivial app is as hard to read as an over-grown one.

### Deleting an app — reversible for 7 days

```bash
curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/apps/" | python3 -m json.tool   # find the id
curl -s -X DELETE -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/apps/<id>"
```

Delete is a **soft delete**: the app is tombstoned and its saved data is kept for
7 days, then purged. Recover within the window with `POST /api/apps/{id}/recover`
(or, for a store app, just reinstall it) — see `recovery.md`. Before deleting:
verify the app exists, tell the partner which one (name, id, description), and
ask for confirmation ("Delete [name]? You can recover it for 7 days."), then
delete. Log creates and deletions to the inbox in the same turn (the id is your
recovery handle); updates skip the log unless they revealed something non-obvious.

---

## Storage — `window.mobius.storage` is the default

Persist app data through `window.mobius.storage` — injected into EVERY mini-app before your module loads, so make it your DEFAULT (not raw `fetch`). It's a read-through wrapper over the storage API: reads are instant (local cache, revalidated in the background) and keep working offline (last-known value overlaid with pending writes — read-your-writes); writes made offline queue and auto-sync on reconnect. Raw `fetch('/api/storage/...')` inside an app has no offline queue/cache and silently drops offline writes.

```jsx
// read: your data, or null if the path is absent (never written/removed/404).
const notes = (await window.mobius.storage.get('notes.json')) || []
// write: pass your data DIRECTLY — do NOT JSON.stringify, the runtime does it.
await window.mobius.storage.set('notes.json', notes)
await window.mobius.storage.remove('items/abc.json')
// TYPED variants for non-JSON data — wrong method on a path throws a clear error (never corrupts):
await window.mobius.storage.setText('doc.md', '# notes')      // raw text (.md/.tex/.csv…)
const md = await window.mobius.storage.getText('doc.md')      // string | null (SWR, offline)
await window.mobius.storage.setBlob('photo.png', fileOrBlob)  // binary, ≤25 MiB, offline-cached
const img = await window.mobius.storage.getBlob('photo.png')  // Blob | null (cache-first, offline)
// subscribeText / subscribeBlob mirror subscribe() for those kinds.
// reactive read: cb fires with the current value, then on every change/sync. Prefer this over re-reading.
const unsub = window.mobius.storage.subscribe('notes.json', v => setNotes(v || []))
// enumerate a directory's immediate children instead of probing filenames:
// [{name,path,type,size,modified_at,mime_type}], [] when empty, null on network failure
// (list() has NO offline mirror, unlike get()).
const entries = await window.mobius.storage.list('items/')
window.mobius.online                        // boolean
await window.mobius.storage.pendingCount()  // unsynced writes — for sync logic only, never rendered as UI
```

Conflict policy is last-write-wins per path; where a lost edit would matter, store one file per record (`items/<uuid>.json`) so concurrent edits to different records don't clobber. `window.mobius.storage` is the easy default, not a cage — an app may use raw IndexedDB / OPFS / its own backend (same-origin iframe); the platform never blocks the escape hatch.

**Any view the agent might write to externally MUST `subscribe()`, not load-on-mount.** A current-session draft, today's log, an inbox — anything the Möbius agent populates from a chat turn while the app sits open — has to use `window.mobius.storage.subscribe(path, cb)` so it repaints when that storage changes under it. A view that only reads once in its mount effect leaves the owner staring at a blank panel after the agent writes (the Workout current-session card was the case). If a view genuinely can't subscribe, tell the owner up front they must reopen or refresh to see agent-written entries — and never claim the shell remounts a mini-app when your turn ends, because there is no such guarantee (the iframe stays in the LRU cache).

### The `.json`-no-envelope trap (silent data loss)

For `.json` storage paths the body IS the document. The envelope form `{content: JSON.stringify(data)}` is NOT unwrapped for `.json` — the server stores the envelope literally, the app loads back `{content:"..."}` instead of its data, falls through to empty state, and the next save overwrites real data with empty. This looks exactly like "the app forgot everything."

- **`.json`** → write `body: JSON.stringify(data)`, read `await res.json()`.
- **Non-`.json`** (markdown, css, html) → use the `{content: "..."}` envelope.
- Prefer `window.mobius.storage` (above), which handles this for you (pass the object, it stringifies).

### Enumerate, don't probe

There is no `HEAD` on storage (it 405s). GET-probing guessed paths (e.g. `reports/<date>.html` for the last 30 days) is the anti-pattern that shipped an app showing empty in prod — you can't know what an app stored by guessing; you enumerate. Use `storage.list('prefix/')` (inside an app) or `GET /api/storage/apps-list/{appId}/{prefix}` / `GET /api/storage/shared-list/{prefix}` (cron/agent). Returns `{entries:[{name,path,type,size,modified_at,mime_type}], next_cursor}` (immediate children only, `?limit=` ≤500, opaque `?cursor=`). `list()` has no offline mirror, unlike `get()`.

### Raw storage API (cron, agent, cross-app `shared/`, non-`.json` blobs)

`window.mobius.storage` only exists inside a running app and is scoped to it. Outside an app, or for `shared/` files, hit the endpoint directly:

```jsx
// GET /api/storage/apps/{appId}/{path} -> 404 if missing, else your data;
// PUT same path, body = your data. /api/storage/shared/{path} for shared files.
const res = await fetch(`/api/storage/apps/${appId}/${path}`, {
  headers: { Authorization: `Bearer ${token}` },
})
```

The extension picks the form (same `.json`-no-envelope rule as above).

### Cross-app feedback

When an app asks the partner for feedback that another agent should notice, write it twice:

- Local app storage: `feedback/<id>.json` via `window.mobius.storage`, so the app owns its audit trail and offline/read-your-writes behavior.
- Shared storage: `app-feedback/<app-slug>/<id>.json` via `PUT /api/storage/shared/...`, best-effort and honestly surfaced if it fails, so Dreaming and future cross-app agents can enumerate it without knowing the app's numeric id.

Use a small structured object: `app`, `kind`, `created_at`, `signal`, `text`, and domain context such as `report_date`, `article_headlines`, `source_id`, or `screen`. Keep one record per file. Consumers must enumerate `shared-list/app-feedback/` and app subfolders; do not probe guessed ids.

---

## App analytics — emit signals for Dreaming

`window.mobius.signal(name, payload?)` is the lightweight analytics hook every app should use. It feeds Dreaming's nightly digest so the agent knows which apps the partner actually used, what errors hit, and where to focus improvement work. Calling it costs nothing at runtime: fire-and-forget, never throws, buffers in memory and flushes at most once per 5 seconds to `signals.jsonl` in the app's own storage.

**Every app must emit at minimum:**

```jsx
// When the component mounts and data has loaded — lets Dreaming count real opens
// (distinct from the platform's own app_open event, which fires on iframe load)
window.mobius.signal('app_ready', { item_count: items.length })

// When the user creates a record
window.mobius.signal('item_created', { type: 'note' })   // type = your domain noun

// When the user deletes a record
window.mobius.signal('item_deleted')

// In error boundaries and catch blocks — the message field is what Dreaming reads
window.mobius.signal('error', { message: err.message, source: 'save' })
```

**Cron-driven apps** replace `item_created`/`item_deleted` with one signal per run:

```jsx
window.mobius.signal('cron_summary', { status: 'ok', items_fetched: 12 })
// or on failure:
window.mobius.signal('cron_summary', { status: 'error', message: err.message })
```

**Payload rules** (enforced by the runtime — violations are dropped silently, not thrown):
- `name`: short kebab-case string (`'app_ready'`, `'item_created'`, `'error'`).
- `payload`: flat object with primitive values only (string, number, boolean). Nested objects and arrays are dropped. No PII.

**No setup required.** `window.mobius.signal` is always available after `init()` — you don't need a token, a storage call, or any initialization. Just call it.

```jsx
// Minimal app_ready example — add to your top-level useEffect after loading data
useEffect(() => {
  window.mobius.storage.subscribe('items.json', (v) => {
    const items = v || []
    setItems(items)
    window.mobius.signal('app_ready', { item_count: items.length })
  })
}, [])
```

Signals land in `signals.jsonl` in the app's own storage path, readable by Dreaming via the storage API. Dreaming's nightly `per-app-digest.json` counts signal names within 24h and surfaces the last 5 error messages — it does not read the raw file. The raw file is only read for the digest build; nothing else touches it.

---

## No native dialogs

The sandbox excludes `allow-modals`, so native `window.confirm/alert/prompt` silently no-op — `confirm` returns `false`, `prompt` returns `null`. A delete-confirm that always returns false looks like a broken feature. Build in-app modal components for confirmations and inputs (see the app-store `ConfirmModal` pattern), themed with the CSS variables below.

---

## Design conventions

Mini-apps should look like they belong to the shell, and like each other. The
**full canonical shape** for every recurring block (header, sheet, card,
button, empty state, segmented control, offline pill, …) lives in
[app-component-shapes.md](app-component-shapes.md) — `Read` it when building or
restyling UI and **copy the blocks you need**. The rules behind those shapes:

- **One scoped stylesheet, not inline-style objects.** Declare a module-level
  ``const CSS = `...` `` and render it once at the app root as
  `<style>{CSS}</style>`. Style via semantic classNames (`className="ma-card"`).
  Use the inline `style={}` prop ONLY for values computed at render time (a
  measured height, a drag transform, a per-row color). Inline objects cannot
  express `:hover`/`:focus`/`:active`, media queries, `@keyframes`, or
  pseudo-elements — that's the friction wall that stops an app from growing.
  The app runs in its own iframe, so the `<style>` is scoped to your app
  automatically; no CSS Modules, no BEM. (`app-latex` and `mind` are the
  cleanest references — both already do exactly this.)
- **Naming.** A short per-app class prefix (`mg-` mind, `cb-` atlas, a 2–3-char
  mnemonic for yours) + semantic kebab roles (`ma-header`, `ma-sheet`,
  `ma-btn`). States via REAL pseudo-classes (`:hover`, `:disabled`,
  `:focus-visible`). App-driven state CSS can't read uses an `is-`/`has-`
  modifier class (`.ma-card.is-selected`). **Never** a `tab(active)` /
  `card(variant)` JS helper that returns a style object — it hides state in JS
  and blocks reuse.
- **Color is always a theme token** so the app follows light/dark:
  `--bg --surface --surface2 --text --muted --accent --accent-fg --accent-hover
  --accent-dim --border --border-light --danger --green --font --mono`. There
  is **no `--red`** (use `--danger`) and **no `--fg`** (use `--text` — a
  `var(--fg,#111)` fallback is invisible in dark mode). Text/icons on an
  accent or danger FILL use `var(--accent-fg)` with **no fallback hex** — it's
  the one legal foreground there, replacing the old `#fff`/`#0d0d0d`/`#062016`
  hardcodes that a custom theme would break. Hardcoded hex only for an
  app-specific accent the theme can't express (a brand color, a chart series).
  To read a token in JS, resolve it live
  (`getComputedStyle(document.documentElement).getPropertyValue('--accent')`).
- **Touch + radius.** Every interactive control `min-height: 44px`; every
  icon-only button gets an `aria-label`. Radius scale: 8px inputs/small,
  10–12px cards/primary buttons, 16px sheet top. Don't invent per-app radii.
- **No native dialogs.** The sandbox has no `allow-modals`, so
  `confirm/alert/prompt` silently no-op. Use the bottom-sheet (the canonical
  dialog; a centered card is an allowed variant only for a tiny confirm).
  Scrims/sheets/toasts are `position: absolute` anchored to the app root
  (which is `position: relative`), never `fixed` — a `fixed` overlay can paint
  over the shell's own chrome.
- **Empty states.** Every list/feed/graph gets a 3-part empty state (mark +
  one-line title + one-line subtitle), never a bare "Nothing here."
- **Sync status is SILENT WHEN HEALTHY — never show "Saving" or pending-write
  counters while online.** `window.mobius.storage` queues writes safely; that's
  invisible plumbing, not information. When offline, show a plain "Offline"
  pill (no counts, no timestamps); an error the owner must act on may surface,
  plainly worded. See the SyncPill shape in
  [app-component-shapes.md](app-component-shapes.md).
- **Keep copies consistent with fence comments.** Each shared block is wrapped
  in an IDENTICAL versioned marker across apps, so divergence is visible and a
  future extraction into a real library is a `grep`-and-move:
  `/* mobius-ui:Header v1 — keep in sync; library candidate. */` … `/* /mobius-ui:Header */`.
  Keep the class names + markup inside a fence identical across apps; diverge
  the *shape* when your app genuinely needs to, but never *rename* a shared
  class for no reason (that breaks the extraction seam). We're not building the
  shared library yet — consistent copies are correct until ~3 apps prove a
  block identical, at which point the lift is mechanical.
- **Scheduled apps — never a dead time-picker.** If the cron cadence is NOT
  user-editable, show it in words ("Updates daily") plus "ask the Möbius agent
  to reschedule" — don't render a picker that writes a file nothing reads. If
  it IS editable, ship a `sync-cron.sh` that actually rewrites the crontab (see
  `cron.md`). Lead with the cadence either way.

### Canonical single-file app skeleton

Start every new app from this shape: ``const CSS`` rendered via
`<style>{CSS}</style>`, fenced shared blocks, theme tokens, 44px controls, a
header with a mark + title + subtitle, a list with a 3-part empty state, and a
bottom-sheet. Copy more blocks (cards, segmented control, offline pill) from
[app-component-shapes.md](app-component-shapes.md). Pick your own short class
prefix (here `ma-`). Then fill in the domain logic.

```jsx
import { useState, useEffect } from 'react'

const CSS = `
/* mobius-ui:Root v1 — keep in sync; library candidate. */
.ma-root { position: relative; display: flex; flex-direction: column; height: 100%;
  overflow: hidden; background: var(--bg); color: var(--text); font-family: var(--font); }
.ma-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 14px 16px 32px;
  display: flex; flex-direction: column; gap: 8px; }
/* /mobius-ui:Root */

/* mobius-ui:Header v1 — keep in sync; library candidate. */
.ma-header { flex: 0 0 auto; display: flex; align-items: center; justify-content: space-between;
  gap: 12px; min-height: 48px; padding: 12px 16px; background: var(--surface);
  border-bottom: 1px solid var(--border); }
.ma-brand { display: flex; align-items: center; gap: 11px; min-width: 0; }
.ma-mark { flex: 0 0 auto; width: 30px; height: 30px; border-radius: 9px; display: flex;
  align-items: center; justify-content: center; font-size: 16px; font-weight: 700;
  background: color-mix(in srgb, var(--accent) 16%, transparent); color: var(--accent); }
.ma-title { margin: 0; font-size: 18px; font-weight: 700; letter-spacing: -0.015em; }
.ma-subtitle { display: block; margin-top: 2px; font-size: 12px; color: var(--muted); }
/* /mobius-ui:Header */

/* mobius-ui:Empty v1 — keep in sync; library candidate. */
.ma-empty { display: flex; flex-direction: column; align-items: center; text-align: center;
  gap: 8px; margin: auto; padding: 48px 24px; color: var(--muted); }
.ma-empty-mark { width: 64px; height: 64px; margin-bottom: 10px; border-radius: 18px; display: flex;
  align-items: center; justify-content: center; font-size: 30px;
  background: color-mix(in srgb, var(--accent) 14%, transparent); }
.ma-empty-title { font-size: 17px; font-weight: 700; color: var(--text); }
.ma-empty-text { margin: 0; font-size: 14px; line-height: 1.6; }
/* /mobius-ui:Empty */

/* mobius-ui:Card v1 — keep in sync; library candidate. */
.ma-card { background: var(--surface); border: 1px solid var(--border); border-radius: 12px;
  padding: 14px 16px; }
/* /mobius-ui:Card */

/* mobius-ui:Button v1 — keep in sync; library candidate. */
.ma-btn { display: inline-flex; align-items: center; justify-content: center; min-height: 44px;
  padding: 10px 16px; border-radius: 10px; border: 1px solid var(--border); background: var(--surface);
  color: var(--text); font-family: var(--font); font-size: 14px; font-weight: 600; cursor: pointer;
  transition: background .14s ease, border-color .14s ease, transform .1s ease; }
.ma-btn:active { transform: scale(0.97); }
.ma-btn:disabled { opacity: 0.5; cursor: default; }
.ma-btn-primary { background: var(--accent); border-color: var(--accent); color: var(--accent-fg); }
.ma-btn-secondary { background: var(--surface2, var(--surface)); }
/* /mobius-ui:Button */

/* mobius-ui:Input v1 — keep in sync; library candidate. */
.ma-input { display: block; width: 100%; box-sizing: border-box; min-height: 44px; padding: 11px 12px;
  background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 8px;
  outline: none; font-family: var(--font); font-size: 16px; }
.ma-input:focus { border-color: var(--accent); box-shadow: 0 0 0 1px var(--accent); }
/* /mobius-ui:Input */

/* mobius-ui:Sheet v1 — keep in sync; library candidate. */
.ma-scrim { position: absolute; inset: 0; z-index: 100; display: flex; align-items: flex-end;
  justify-content: center; padding: 16px; background: rgba(0,0,0,0.5); }
.ma-sheet { width: 100%; max-width: 480px; padding: 24px; background: var(--surface);
  border: 1px solid var(--border); border-radius: 16px 16px 0 0; display: flex; flex-direction: column; gap: 12px; }
.ma-sheet-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 12px; }
.ma-sheet-actions .ma-btn { flex: 1; }
/* /mobius-ui:Sheet */
`

export default function MyApp({ appId, token }) {
  const [items, setItems] = useState([])
  const [adding, setAdding] = useState(false)
  const [draft, setDraft] = useState('')

  useEffect(() => window.mobius.storage.subscribe('items.json', (v) => setItems(v || [])), [])

  async function save() {
    if (!draft.trim()) return
    const next = [...items, { id: crypto.randomUUID(), text: draft.trim() }]
    await window.mobius.storage.set('items.json', next)  // pass the object directly — no JSON.stringify
    setDraft(''); setAdding(false)
  }

  return (
    <div className="ma-root">
      <style>{CSS}</style>
      <header className="ma-header">
        <div className="ma-brand">
          <span className="ma-mark" aria-hidden="true">M</span>
          <div>
            <h1 className="ma-title">My App</h1>
            <span className="ma-subtitle">One-line description of what it does</span>
          </div>
        </div>
        <button className="ma-btn ma-btn-primary" onClick={() => setAdding(true)}>+ New</button>
      </header>

      {items.length === 0 ? (
        <div className="ma-empty">
          <div className="ma-empty-mark" aria-hidden="true">✶</div>
          <div className="ma-empty-title">Nothing yet</div>
          <p className="ma-empty-text">Tap “+ New” to add your first item.</p>
        </div>
      ) : (
        <div className="ma-scroll">
          {items.map((it) => <div key={it.id} className="ma-card">{it.text}</div>)}
        </div>
      )}

      {adding && (
        <div className="ma-scrim" onClick={() => setAdding(false)} role="dialog" aria-modal="true">
          <div className="ma-sheet" onClick={(e) => e.stopPropagation()}>
            <h3 className="ma-sheet-title">New item</h3>
            <input className="ma-input" value={draft} onChange={(e) => setDraft(e.target.value)} autoFocus />
            <div className="ma-sheet-actions">
              <button className="ma-btn ma-btn-secondary" onClick={() => setAdding(false)}>Cancel</button>
              <button className="ma-btn ma-btn-primary" onClick={save}>Save</button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
```

---

## Libraries

The canonical bare-specifier runtime-lib manifest lives at `backend/app/runtime_libs.py`. The `app-frame.html` import map provides those libraries so they load fast and cache across apps.

**`three` is self-hosted via the import map — use the bare specifier:**

```jsx
import * as THREE from 'three'
import { OrbitControls } from 'three/addons/controls/OrbitControls.js'
```

Never hardcode `/vendor/three@<version>/…` — a three bump then 404s the build → SPA HTML fallback → "failed to load dynamic module". The bare `'three'` specifier is version-proof; the import map points at the pinned version.

**Any other library is fair game via runtime dynamic import from esm.sh** (no install, no build step):

```jsx
const { DndContext } = await import('https://esm.sh/@dnd-kit/core')   // drag-and-drop
const L = (await import('https://esm.sh/leaflet')).default            // maps
const { motion } = await import('https://esm.sh/framer-motion')       // animations
```

**To add a library to the import map permanently** (faster, no per-load dynamic import), edit `/data/shell/public/app-frame.html` — the backend prefers this copy over the baked fallback, so changes take effect on the next app load with no shell rebuild. Add an entry like `"@dnd-kit/core": "https://esm.sh/@dnd-kit/core@6",`.

---

## Offline-capable apps (opt-in)

Storage already works offline via `window.mobius.storage` (above) for every app. `offline_capable: true` is a SEPARATE flag that ADDS caching of the app's own CODE — the service worker caches the frame + module so the app loads and renders with no network. Set it in the create or PATCH body for any app that genuinely works offline (notes, a tracker, a game).

Separately, and automatically for EVERY app (no flag), the shell's service worker keeps an installed PWA out of the browser's native "no internet" page: a non-offline-capable app shows a branded offline screen when opened offline, never browser chrome. So the flag is the difference between "the real app runs offline" (set it) and "a branded you're-offline screen" (the automatic default) — neither ever drops to the browser error page.

Only set `offline_capable` when the app genuinely works offline. A network-dependent app marked offline-capable caches stale/empty state and looks broken — leave those at the default.

---

## Fetching external URLs

Mini-apps cannot fetch external URLs directly (CORS). Use the proxy:

```jsx
const res = await fetch(`/api/proxy?url=${encodeURIComponent(url)}`, {
  headers: { Authorization: `Bearer ${token}` },
})
```

A non-self external resource referenced at load time — a Google Fonts `<link>`/`@import`, any off-origin CDN script or stylesheet — can do worse than fail silently under the `default-src 'self'` CSP: it can HANG the in-app browser so the page never finishes loading and the whole app goes non-interactive (taps and anchors dead, "loading timeout"). So when the owner reports BOTH "X doesn't work" AND "loading timeout" in the same breath, treat the hang as the primary signal and grep the app for off-origin references first — it is not a scroll/offset bug. Vendor the font/asset same-origin or drop it; bundle fonts as a `@font-face` over a self-hosted file, never a CDN link.

---

## Agent-powered mini-apps

Apps that need agent assistance embed the real shell chat with one call —
`window.mobius.chat(...)` owns the whole lifecycle, so do NOT hand-roll the
`POST /api/app-chats` create, the `PATCH` prompt update, or the chat-id
persistence yourself:

```jsx
const mountRef = useRef(null)
useEffect(() => {
  const mount = mountRef.current
  if (!mount) return
  let handle
  let disposed = false
  window.mobius.chat({
    mount,
    persist: 'chat_id.json',   // create the app-chat ONCE, save its id here, reuse it forever
    systemPrompt,              // shapes the chat on create + re-applies on resume
    picker: false,             // hide the provider/effort picker inside the app sheet
    onTurnDone: () => refresh(),   // a turn finished — reload app state
  }).then((h) => { if (disposed) h.destroy(); else handle = h })
  return () => { disposed = true; handle?.destroy() }
}, [/* stable deps */])
// ...
<div ref={mountRef} style={S.chatMount} />
```

- `persist` makes the helper create the chat the first time and **reuse the same
  one** on every later mount (PATCHing `systemPrompt` on resume) — the persistent
  transcript the user expects. Omit it only for a throwaway chat.
- `onReady` / `onTurnDone` / `onMessageSent` / `onError` are wired before the
  embed mounts, so they never miss an event. `onTurnDone` is where you refresh.
- **Viewer variant:** to display an EXISTING chat the app didn't create (e.g. a
  cron-attributed daily chat resolved from a `meta.json`), pass an explicit
  `chatId` and no `persist` — the helper just mounts it read-through.
- Keep the chat as the interaction surface; it gives the user a persistent
  transcript, normal agent tooling, and follow-up questions in one place.

---

## Communicating with the shell

```jsx
// Open a new chat with pre-filled text
window.parent.postMessage({ type: 'moebius:new-chat', draft: 'Hello!' }, window.location.origin)
```

---

## Token scoping

Mini-apps receive a scoped token (not the owner's full JWT). It CAN access: storage, proxy, app-attributed chats, notifications, push, uploads, and app endpoints. It CANNOT access: auth, settings, or owner-only chat endpoints.

---

## Back-gesture support (on-demand — skip unless your app has internal navigation)

Most mini-apps don't need any of this. **Skip unless your app has drill-downs, modals, or nested views.**

For simple internal navigation, use the runtime helper. It asks the shell to
install a real top-level back target, then calls your callback when the user
uses device/browser back:

```jsx
const navRef = useRef(null)

async function openDetail(item) {
  const handle = window.mobius.nav.open('app-detail', () => {
    navRef.current = null
    setSelected(null)
  })
  navRef.current = handle
  await handle.ready
  if (navRef.current !== handle) return
  setSelected(item)
}

function closeDetail() {
  navRef.current?.close()
  navRef.current = null
  setSelected(null)
}
```

### Android back-preview — shell-mediated back protocol

The swipe-back gesture renders a preview of the previous screen from a top-level history snapshot. Iframe `history.pushState` is invisible to that mechanism, so iframe-history-only apps get a blank preview. `window.mobius.nav.open(...)` wraps the shell-mediated protocol below; use the raw postMessage form only for legacy apps or custom choreography.

```jsx
function navPushAndAwaitAck(label) {
  const requestId = `np-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  return new Promise((resolve, reject) => {
    function onMsg(e) {
      if (e.origin !== window.location.origin) return
      if (e.source !== window.parent) return
      if (e.data?.requestId !== requestId) return
      if (e.data.type === 'moebius:nav-push-ack') {
        window.removeEventListener('message', onMsg); resolve()
      } else if (e.data.type === 'moebius:nav-push-rejected') {
        window.removeEventListener('message', onMsg); reject(new Error('nav-push rejected (cap hit)'))
      }
    }
    window.addEventListener('message', onMsg)
    window.parent.postMessage({ type: 'moebius:nav-push', label, requestId }, window.location.origin)
  })
}

async function openArticle(article) {
  try { await navPushAndAwaitAck('klix-article') } catch { return }  // shell rejected; stay on the list
  setSelectedArticle(article)  // safe to render the nested view now
}

// On the app's own in-app back tap (X button, swipe handler):
window.parent.postMessage({ type: 'moebius:nav-pop' }, window.location.origin)

// Listen for the shell to tell you the user back-gestured:
useEffect(() => {
  function onMessage(e) {
    if (e.origin !== window.location.origin) return
    if (e.source !== window.parent) return
    if (e.data?.type !== 'moebius:nav-back') return
    closeNestedView()  // your app's own state mutation
  }
  window.addEventListener('message', onMessage)
  return () => window.removeEventListener('message', onMessage)
}, [])
```

(Vanilla-JS variant: same messages, same origin checks, just `await new Promise(...)` and plain functions instead of React hooks.)

The shell installs a back-sentinel in its own history on `nav-push`, so the OS snapshots the list page underneath for the preview; on back-gesture the shell consumes the sentinel and forwards `moebius:nav-back` to you instead of changing its own view. Single back-press, real preview.

**Rules of the protocol:**

- Pick ONE model per nested-view level — combining `iframe.history.pushState` with this protocol scrambles the back stack.
- `nav-pop` and `nav-push` are a strict pair, like push/pop on a stack. Every code path that exits a nested view (including the in-app X button) MUST call `nav-pop`; skip it and the next back-gesture is silently consumed by the host.
- The host caps pending sentinels at 20 per app. On overflow it responds `{type:'moebius:nav-push-rejected', requestId}` — the helper above rejects its promise, so you simply don't render the nested view. If you bypass the helper, treat a rejection as a hard "stay where you are" and do NOT increment your local counter, or your count drifts above the host's permanently and the next `nav-pop` consumes the wrong sentinel.
- The `requestId` is optional on the wire (the shell echoes whatever you send), but use a fresh id per push when multiple can be in flight — a stale ack can otherwise resolve a later promise.

**Across app switches:** app-sentinels are preserved across drawer-driven app switches. Nest 2 levels in Klix, drawer-tap to Notes, and the user gets browser-style back (first back returns to Klix showing its nested view, then unwinds Klix, last back exits). Your iframe stays mounted in the LRU cache while invisible, so its state is preserved — just respond to `moebius:nav-back` correctly even when currently invisible (by the time it arrives, your iframe is visible again).

**No tree restoration:** the protocol stores a count, not a stack of labels. If you push 3 sentinels (list → detail → edit) and the host sends 3 `nav-back` events, your app must unwind them in order. Keep your own breadcrumb if the hierarchy is non-trivial.
