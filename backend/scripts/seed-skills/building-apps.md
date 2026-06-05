# Building mini-apps

The full mini-app contract: component shape, `window.mobius.storage` and its traps, the app lifecycle (register-on-create only), offline, fetching, AI, back-navigation, and theming. `Read` this before building or updating any mini-app.

Mini-apps are JSX components in sandboxed iframes. Each gets `appId` and `token` props and persists through `window.mobius.storage`. The iframe is same-origin, so all browser storage works and `fetch('/api/...')` is free; this also means a mini-app can read the owner JWT — an accepted single-owner trade-off, not a license to be careless.

---

## Before building: check existing apps

Default to checking what already exists before creating a new one:

```bash
curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/apps/" | python3 -m json.tool
```

If an app with the same purpose exists, update it instead of duplicating. If the partner says "build X" and X already exists, confirm whether they want to update or replace it.

---

## Packaging a third-party static app from GitHub

For an existing React/Vite/CRA/WebGL game or tool, do **not** copy built files into `/data/shell` or `/data/shell/dist`. Deploy refreshes replace that tree, so the app disappears or, worse, `/some-app/index.html` falls through to the Möbius shell and opens Möbius inside Möbius.

The durable pattern is a tiny Mobius wrapper plus manifest `static_assets`:

1. Clone/fork the upstream repo.
2. Make the app build as a static site with **relative** asset paths whenever the framework supports it (`PUBLIC_URL=.`, `homepage: "."`, Vite `base: "./"`, etc.).
3. Build into `build/` or `dist/`.
4. From the repo root, run:

```bash
node /app/scripts/package-static-app.mjs \
  --id cuberun \
  --name "CubeRun" \
  --version "1.0.0-mobius.1" \
  --description "Neon 3D runner game packaged for Mobius." \
  --homepage "https://github.com/mobius-os/app-cuberun" \
  --build-dir build \
  --out-dir . \
  --icon icon.png
```

The packager writes `mobius.json` and an iframe `index.jsx`, enumerates every build file as `static_assets`, rewrites root-relative HTML/CSS asset references such as `/static/js/main.js` or `/fonts/foo.ttf` when the target exists inside the build, and fails on unresolved local CSS URLs. Re-run with `--force` after edits. Verify the generated package before pushing:

```bash
node /app/scripts/package-static-app.mjs --help
npm run test:packager --prefix /home/hmzmrzx/projects/mobius
```

After publishing the package repo, install it through the app installer, not by hand-copying into `/data`:

```bash
curl -s -X POST "$API_BASE_URL/api/apps/install" \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"manifest_url":"https://raw.githubusercontent.com/mobius-os/app-cuberun/main/mobius.json"}'
```

Runtime smoke checks for this class of app:

- `/app-assets/by-id/<app-id>/index.html` returns the actual static app HTML.
- `/app-assets/by-id/<app-id>/static/...` and fonts/media return 200.
- Old ad-hoc routes such as `/cuberun/index.html` return 404, not the shell HTML.
- Opening `/shell/?app=<app-id>` shows the game/tool inside the nested iframe, not a copy of Möbius.

If a package update leaves `.mobius-bak` files or a dirty `/data/apps/<slug>` git tree, that is installer noise, not app source; re-run the installer on a backend that includes the static-asset backup fix.

---

## Component shape

```jsx
export default function MyApp({ appId, token }) {
  return <div>...</div>
}
```

---

## Lifecycle — register on create, just save to edit

1. Write JSX to `/data/apps/<name>/index.jsx`.
2. **On first create only**, register + compile (mints the id + DB row):

```bash
python "$SCRIPTS_DIR/register_app.py" "<name>" "<description>" /data/apps/<name>/index.jsx
```

`register_app.py` reads `$CHAT_ID` from the environment and stores it with the app so crash reports route back to this chat.

**For edits, just write the file — do NOT re-run `register_app.py`.** A file watcher recompiles `/data/apps/<slug>/index.jsx` ~1s after you save. Re-running the script creates a DUPLICATE every time the name differs by a character (slug-vs-title is the common slip). If the partner says it didn't change, check that `/data/compiled/app-<id>.js` mtime advanced and look for `compile failed for` in `/data/logs/chat.log` — a JSX syntax error blocks the recompile. If a duplicate appears, `DELETE /api/apps/<dup-id>`.

**Use `register_app.py`, not raw `curl POST /api/apps/`.** The raw endpoint requires an undocumented `jsx_source` field (422 without it); updates are `PATCH` not `PUT` (405). The helper handles all of this — skipping it burns tool calls rediscovering the schema from error responses.

### Deleting an app — permanent, no recovery

```bash
curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/apps/" | python3 -m json.tool   # find the id
curl -s -X DELETE -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/apps/<id>"
```

Before deleting: verify the app exists, tell the partner which one (name, id, description), ask for explicit textual confirmation ("Are you sure you want to delete [name]? This cannot be undone."), and only delete after they confirm. Log creates and deletions to the inbox in the same turn; updates skip the log unless they revealed something non-obvious.

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
await window.mobius.storage.pendingCount()  // unsynced writes
```

Conflict policy is last-write-wins per path; where a lost edit would matter, store one file per record (`items/<uuid>.json`) so concurrent edits to different records don't clobber. `window.mobius.storage` is the easy default, not a cage — an app may use raw IndexedDB / OPFS / its own backend (same-origin iframe); the platform never blocks the escape hatch.

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

## No native dialogs

The sandbox excludes `allow-modals`, so native `window.confirm/alert/prompt` silently no-op — `confirm` returns `false`, `prompt` returns `null`. A delete-confirm that always returns false looks like a broken feature. Build in-app modal components for confirmations and inputs (see the app-store `ConfirmModal` pattern), themed with the CSS variables below.

---

## Design conventions

Mini-apps should look like they belong to the shell. Several real bugs came from drifting off these — follow them by default:

- **Status colors:** the app frame defines exactly two status tokens — `var(--danger)` for errors and `var(--green)` for success. There is **no `--red`** (using it silently falls back to a hardcoded hex and never picks up the theme). Everything else uses `--accent`, `--text`, `--muted`, `--bg`, `--surface`, `--border`.
- **Touch targets:** mobile is the primary target — every interactive control gets `min-height: 44px` and every icon-only button gets an `aria-label`. Larger targets don't hurt desktop.
- **One inline-style object named `S`** (`const S = { ... }`), consistently, so apps read alike.
- **Scheduled apps — never a dead time-picker.** If the cron cadence is NOT user-editable, show it in words ("Updates daily") plus "ask the Möbius agent to reschedule" — don't render a picker that writes a file nothing reads. If it IS editable, ship a `sync-cron.sh` that actually rewrites the crontab (see `cron.md`). Lead with the cadence either way.

### Theme-aware colors

**Use CSS variables for structural elements** (backgrounds, text, borders, cards, inputs) so apps work in both light and dark mode. Hardcoding `#0c0f14` instead of `var(--bg)` breaks the app when the partner toggles modes — half their devices are in the mode you didn't test. Hardcoded colors are fine only for app-specific accents (a brand color, a chart series).

```jsx
const S = {
  root:  { padding: '16px', height: '100%', overflow: 'auto',
           background: 'var(--bg)', color: 'var(--text)', fontFamily: 'var(--font)' },
  btn:   { background: 'var(--accent)', color: '#fff', border: 'none',
           borderRadius: '6px', padding: '8px 16px', cursor: 'pointer' },
  card:  { background: 'var(--surface)', border: '1px solid var(--border)',
           borderRadius: '8px', padding: '12px 16px' },
  input: { background: 'var(--surface)', border: '1px solid var(--border)',
           borderRadius: '6px', color: 'var(--text)', padding: '8px 12px', outline: 'none' },
}
```

Variables: `--bg`, `--surface`, `--surface2`, `--text`, `--muted`, `--accent`, `--accent-hover`, `--accent-dim`, `--border`, `--border-light`, `--danger`, `--green`, `--font`, `--mono`. They adapt automatically when the partner toggles light/dark. Don't invent fallbacks like `var(--fg, #111)` — there is no `--fg` and a near-black fallback is invisible in dark mode. If you must read a token in JS, resolve it live (`getComputedStyle(document.documentElement).getPropertyValue('--accent')`) rather than hardcoding a hex twin that won't follow the theme.

### Modals, scrims & radii

In-app modals are the only option (no native dialogs). Standardize the overlay so every app reads the same — the App Store app is the reference:

- **Placement: a bottom sheet.** Backdrop `position: fixed; inset: 0; display: flex; align-items: flex-end; justify-content: center`; panel `width: 100%; max-width: 480px; border-radius: 16px 16px 0 0; padding: 24px`. Bottom sheets are thumb-reachable on a phone; a centered dialog or a left/right slide-in is NOT the house style for confirmations. (A persistent full-height side panel is fine only for a detail inspector — e.g. a graph node — never for a confirm/input.)
- **Scrim:** `rgba(0,0,0,0.6)`. Tapping the scrim cancels, unless a write is in flight.
- **Radius scale:** `8px` inputs + small buttons, `10–12px` cards + primary buttons, `16px` sheet top. Don't invent per-app radii.

### Empty states

Every list / feed / graph gets a real empty state, never a bare muted string. Three parts, centered in the scroll area: a small icon or letter mark, a one-line **title** ("No briefs yet"), and a one-line **subtitle** that says what will fill it ("Möbius writes one each morning"). A blank panel or a lone "Nothing here." reads as broken.

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

---

## AI-powered mini-apps

POST `/api/ai` with `{messages, system, tools}` and stream the SSE body — parse `data: ` lines as JSON and yield each event.

- `tools: false` — text only (chat mode)
- `tools: true` — stateless one-shot sub-agent with the full allowlist (`Bash, Read, Write, Edit, Glob, Grep` — no skill file, no resume)
- `tools: ["Read", "Glob"]` — a list grants only that subset (intersected against the full allowlist; unknown names rejected)
- Events: `{ type: 'text', content }`, `{ type: 'done' }`, `{ type: 'error', message }`

---

## Communicating with the shell

```jsx
// Open a new chat with pre-filled text
window.parent.postMessage({ type: 'moebius:new-chat', draft: 'Hello!' }, window.location.origin)
```

---

## Token scoping

Mini-apps receive a scoped token (not the owner's full JWT). It CAN access: storage, proxy, AI, notifications, push, uploads, app endpoints. It CANNOT access: auth, settings, or chat endpoints.

---

## Back-gesture support (on-demand — skip unless your app has internal navigation)

Most mini-apps don't need any of this. **Skip unless your app has drill-downs, modals, or nested views.**

For simple internal navigation, use `history.pushState` on descent and `popstate` to go back:

```jsx
function goToDetail(id) {
  history.pushState({ detail: id }, '')
  setView('detail')
}
useEffect(() => {
  function onPop() { setView('list') }
  window.addEventListener('popstate', onPop)
  return () => window.removeEventListener('popstate', onPop)
}, [])
```

### Android back-preview — shell-mediated back protocol

The swipe-back gesture renders a preview of the previous screen from a top-level history snapshot. Iframe `history.pushState` is invisible to that mechanism, so iframe-history-only apps get a blank preview. To get a real preview AND single-step back, opt into the shell-mediated protocol via postMessage. The push is a handshake: send `nav-push` with a fresh `requestId`, wait for `moebius:nav-push-ack` with the same id, THEN render the nested view. Opening optimistically lets the OS snapshot the nested view as the back-preview background — the gesture works but the preview shows the screen you're leaving.

```jsx
function navPushAndAwaitAck(label) {
  const requestId = `np-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  return new Promise((resolve, reject) => {
    function onMsg(e) {
      if (e.origin !== window.location.origin) return
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
