# Mini-app quickstart — ordinary local apps

Use this skill for the common case: creating or making a straightforward
update to a local Möbius mini-app under `/data/apps/<slug>/`. It is the fast
path for a focused app with ordinary JSX, app-scoped storage, and no unusual
host integration.

Do **not** read the full `building-apps.md` as well unless the request actually
needs one of its advanced paths:

- wrapping or packaging an existing site/game;
- an installable or upstream-tracked app;
- external fetching/proxying or a separate service;
- secrets, cross-app access, concurrent writers, or raw file storage;
- microphone/device capabilities;
- an embedded agent, immersive mode, or internal navigation/back handling.

For an ordinary one-screen or small multi-card app, this file is the complete
workflow. Do not re-read it later in the same turn.

## Product target: fast, useful, and delightful

Speed to the first usable preview is part of the product. Build one coherent,
visually intentional primary interaction and register it as soon as it works.
Then refine it while the partner can see and try the live app.

The first slice is not a blank shell or wireframe. It should already have:

- one real working interaction;
- deliberate hierarchy, spacing, typography, and colour;
- responsive layout and 44px touch targets;
- visible focus states and reduced-motion handling;
- a meaningful empty/default state;
- a small, focused feature set.

Polish the core interaction; do not delay the preview for secondary features,
packaging research, speculative screens, or exhaustive checks.

For a one-screen app, keep the first registered draft compact. If it is growing
toward roughly 500 lines before the partner can see it, cut secondary controls,
extra presets, elaborate completion effects, and nonessential saved settings.
One beautiful working interaction visible sooner is better than a complete
product hidden for another minute. Add only the refinements that materially
improve the requested experience after registration.

## The common sequence

### 1. Check only what can change the decision

Read the live app list once and update an existing app with the same purpose
instead of duplicating it:

```bash
curl -fsS -H "Authorization: Bearer $AGENT_TOKEN" \
  "$API_BASE_URL/api/apps/" |
  python3 -c 'import json,sys; print([(a["id"],a["name"],a.get("slug")) for a in json.load(sys.stdin)])'
```

Do not search GitHub or the App Store for a uniquely named personal app.
Search the wider ecosystem only when the partner asked for something
installable/shareable, named an existing product, or the request is likely to
match a shipped app exactly.

### 2. Create the first usable slice

Create `/data/apps/<slug>/` with:

```text
index.jsx
mobius.json
README.md
icon.png
.gitignore
```

Initialize the app directory as its own repository before any Git status or
commit command:

```bash
git -C /data/apps/<slug> init -b main
```

The entry shape is:

```jsx
export default function App({ appId, token }) {
  return <div>{/* app */}</div>
}
```

Keep a single module-level stylesheet and render it once:

```jsx
const CSS = `
  * { box-sizing: border-box; }
  .my-root {
    min-height: 100%;
    color: var(--text);
    background: var(--bg);
    font-family: var(--font);
  }
`
```

Use Möbius theme variables (`--bg`, `--surface`, `--surface-2`, `--text`,
`--muted`, `--border`, `--accent`, `--font`). Add a short app-specific prefix
to classes. Prefer CSS classes over inline style objects except for truly
computed values. Use lucide icons already supported by the app runtime rather
than hand-drawn SVG.

For an ordinary app, make one strong visual decision—a calm breathing orb, a
bright decision wheel, a tactile stack of cards—then support it with restrained
chrome. Visual richness should come from hierarchy, composition, motion, and
state feedback, not extra screens.

The manifest should truthfully describe the local app. A typical private,
offline-safe app uses:

```json
{
  "id": "my-app",
  "name": "My App",
  "version": "0.1.0",
  "description": "One useful sentence.",
  "entry": "index.jsx",
  "icon": "icon.png",
  "offline_capable": true,
  "permissions": {},
  "source_files": []
}
```

List every imported sibling source file in `source_files`. Set
`offline_capable` to `true` only when every required read and write works
without the network. The registration helper applies this flag and the
versioned `capabilities` object; do not patch the app row separately.

### 3. Register once, early

As soon as the first slice compiles and contains one real feature:

```bash
python "$SCRIPTS_DIR/register_app.py" \
  "<Name>" "<Description>" /data/apps/<slug>/index.jsx
```

The helper returns the app JSON, including its numeric ID, and opens the live
preview beside the owning chat. Do not send a separate `open_item`.

Registration is for creation only. Afterward, edit source files normally; the
watcher recompiles and refreshes the open preview. Never re-register routine
edits.

The visible intent sentence at the start of the turn is the first progress
cue. Use `build_phase.py` only when the partner benefits from a real milestone:

- the first preview is available;
- verification/refinement has begun and substantial work remains;
- the build is complete.

Combine a phase signal with a tool call doing real work. Do not add a separate
tool call merely to announce an internal step.

### 4. Use app-scoped storage only when the feature needs it

For ordinary private records:

```js
const store = window.mobius?.storage
const value = await store.get('state.json')       // parsed JSON or null
await store.set('state.json', nextValue)          // value itself; no envelope
```

Handle storage failure visibly and keep a usable in-memory fallback. Subscribe
when an already-open app must repaint after agent or cross-frame writes:

```js
const unsubscribe = store.subscribe('state.json', setValue)
```

Do not probe guessed keys. If records are split across keys, keep an explicit
index or use `store.list()`. Do not use `localStorage`, IndexedDB, native
`alert`/`confirm`/`prompt`, or owner credentials.

### 5. Verify the rendered app without exploring the whole shell

Read `visual-testing.md` once before the first browser check. Use the
readiness-gated helper:

```bash
bash "$SCRIPTS_DIR/preview_app.sh" <app-id>
```

It waits for the app frame to mount, captures into the chat media directory,
and prints the embed line. View that PNG before describing it.

For interaction testing, scope the accessibility snapshot to the app frame so
the shell and drawer do not consume the context:

```bash
agent-browser snapshot -i -s 'iframe[data-app-id="<app-id>"]'
```

Use the returned refs, re-snapshot after any DOM change, and verify one complete
primary flow plus persistence when the app saves data. Do not use
`wait --text` for iframe content; it observes the top-level document. The
preview helper already gates the initial frame readiness.

Check the partner's actual viewport first. Add one phone-sized check only when
the supplied viewport is not already phone-sized or the layout materially
changes on small screens. Keep only screenshots that show a useful distinct
state—never a loader, drawer transition, or redundant verification frame.

The default visual-test budget for an ordinary one-screen app is:

1. one readiness-gated preview that you view;
2. one batched primary-flow interaction, followed by a scoped snapshot and
   console/error check;
3. one responsive check when needed.

Do not call browser `--help` during this path; the commands above and
`visual-testing.md` are the contract. Do not test every secondary control or
capture the same state twice. If a check exposes a real defect, fix it and
repeat only the affected check.

### 6. Close in one pass

Run validation once:

```bash
python "$SCRIPTS_DIR/validate-app.py" /data/apps/<slug>
```

Commit only the app source you created:

```bash
git -C /data/apps/<slug> add index.jsx mobius.json README.md icon.png .gitignore
git -C /data/apps/<slug> commit -m "Build <short app purpose>"
```

If the watcher already committed an edit, a clean app-repo status is success;
inspect that app repo's log rather than the parent `/data` repository.

Send the app-complete notification using `notifications.md`, embed the useful
render before describing it, state what the app does, and invite optional
adjustments without blocking the completed turn.

Only when `contributing.md` appears in the session's **Installed app skills**
and the work is plausibly reusable by other Möbius users, offer to prepare it
in Contribute. Do not read the full contribution skill or search upstream
merely to make that offer; read it only if the partner asks to proceed.

## Stop and load the advanced skill when needed

Switch to `building-apps.md` if the common path stops being truthful—for
example, the app needs a proxy, a service, cross-app permissions, secrets,
concurrent writes, packaged assets, an embedded agent, device capabilities,
immersive mode, or a shell-mediated back stack. Name the new requirement before
loading the advanced procedure; do not silently improvise a parallel mechanism.
