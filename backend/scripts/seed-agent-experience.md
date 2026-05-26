# Experience

## About this file

This is the agent's memory across sessions. It's a real file at
`/data/shared/agent-experience.md`. The block you're reading now is
a snapshot taken at the start of this session; another session may
have edited the real file since.

**To append** (the common case — new app, gotcha, preference):

```bash
echo '- Built **Name** (id N). <one-line description>' >> /data/shared/agent-experience.md
```

Use `Bash >>` — it bypasses the Read-before-modify constraint that
`Edit` and `Write` have.

**To read the latest** mid-session: `Read` the path above.

**To delete an outdated entry**: `Read` the file, then `Edit` to
remove the stale line.

**Rules:**
- New entries go at the bottom.
- If newer entries conflict with older ones, the newer entry is
  correct — trust recent information over stale.
- If an entry is outdated or irrelevant, delete it. Stale entries
  mislead future sessions.
- Update in the SAME turn as the action, before writing the final
  assistant message.

**What goes in your /data/shared/agent-experience.md (this file):**
- A new app was built → `- Built **Name** (id N). <description>`
- A gotcha or workaround discovered → a one-liner
- A partner preference learned → a one-liner
- Shell / CSS / cron changed → what and why

Whether to mention what you added or removed from the experience
log is your call — sometimes the change is partner-relevant
("logged that the markdown app needs the GFM plugin so next time
it's a one-line install"), sometimes it's pure engineering memory
and would just be noise. Use judgment.

**Before writing your final message**, scan the session for any
infrastructure discoveries, wrong assumptions, or workarounds you
hit along the way. Each one is a gotcha worth logging — future
sessions will thank you.

If during the build you discovered a non-obvious platform contract
(e.g. "the shell paints `--bg` solidly across the viewport, so html
pseudo-element ornaments get hidden") — append one terse line to
the running experience log under a "Platform contracts" heading.
Engineering memory for the next agent, not partner-facing
narration. One line, not a recap.

## Before building: triage the prompt

Three tiers — pick the matching one and act:

1. **Specific app with obvious defaults** ("tip calculator",
   "pomodoro timer", "stopwatch"): build immediately. No
   questions. The category resolves design.
2. **Specific app with material design choices** ("notes app",
   "habit tracker", "markdown previewer"): build with a confident
   default — pick mood, structure, and the 2–3 most useful
   features yourself — and surface 2–3 alternatives in ONE
   message so the partner can redirect after seeing the result.
   **Do not block on approval.** A shipped v1 they can react to
   beats a perfect v1 negotiated up front.
3. **Vibe prompt** ("something fun", "a cool app", "anything
   useful"): mood is a constraint, not a category. Reply with
   2–3 brief options + a tradeoff and let them pick.

The fun/cool/useful trap: those are mood constraints, not
category constraints. They don't tell you what to build. Treating
them as enough specificity leads to building an app the partner
never asked for.

**If you call AskUserQuestion the runner pauses until the partner
answers.** chat.py freezes the turn at the question event — there
is no auto-build on dismissal. Don't fire a question if you'd
rather just build; pick defaults and ship. Only ask when you
genuinely cannot pick a sensible default.

Everything below — register_app.py, screenshots, notifications —
runs *after* you've decided to build, not instead of deciding.

## Before doing something destructive to the partner's data

You can freely create, edit, delete, and recreate anything *you*
made during a build — test fixtures, temp files, sample notes you
typed to verify a flow. Cleaning up after yourself is just good
hygiene.

Before any command or API call that deletes, overwrites,
truncates, resets, migrates, or bulk-edits existing partner-owned
data, stop and ask in chat — unless the data was created by you
in this same turn as a disposable test fixture. Do not infer
consent from usefulness, reversibility, or confidence. When
unsure who owns it, ask.

## Git

`/data/` is one git repo, initialized on first boot, and is your
working directory. After important or substantial changes — anything
you'd want a clean way to undo if it later turned out wrong — commit:

```bash
/data/.pm-commit 'one-line what and why'
```

`.pm-commit` is a tiny wrapper around `git add -A && git commit`
(read `/data/.pm-commit` if you want to see it).

To see what's happened: `git log --oneline -10`.
To undo uncommitted work: `git diff` to inspect, then
`git restore <path>` to revert.

## Experience log

Add new entries at the bottom. Delete outdated ones. No timestamps;
order is implicit.

- Built **Hello World**. A welcome screen with an "ask the agent"
  button that takes the user to chat. The simplest possible starting
  point — proves the app contract works and gives the user somewhere
  to click.

## Querying the file structure

Hand-written directory tables go stale the moment a file is renamed.
Past versions of this seed claimed files like `ChatInput.jsx` that
no longer existed, which sent the agent on dead-end searches and
caused a real envelope-format bug downstream. The replacement:

**`describe-tree.py`** walks a directory and prints
`filename — first-sentence-of-docstring` for each file. Use it
instead of trusting any hardcoded list, including this one:

```bash
python3 /app/scripts/describe-tree.py /data/shell/src/components/ --depth 1 --quiet
python3 /app/scripts/describe-tree.py /app/app/ --depth 1 --quiet
python3 /app/scripts/describe-tree.py /data/apps/ --depth 1 --quiet
```

Convention (load-bearing — the seed expects you to follow it):
**every new file you write starts with a brief docstring or
top-comment** describing what it does in one sentence. Python →
module docstring. JSX/TS → `/* ... */` block. Shell → leading `#`
comments. CSS → leading `/* ... */`. Without this, the next agent
reading describe-tree.py output sees `(no description)` for your
file and has to open it to figure out what it does — exactly the
friction this convention exists to remove.

## Design principles

- Use CSS variables, don't hardcode colors. The full set is in
  `theme.py:DEFAULT_THEME` — `grep var\\(-- frontend/src/` for live usage.
  Don't invent fallbacks like `var(--fg, #111)` — there is no `--fg`,
  and a near-black fallback is invisible on dark mode.
- `/data/shared/theme-mode` tells you light vs dark.
- Trust the actual viewport over the mobile-first default. Desktop
  layouts on desktop.

## Before writing a theme

Read `Shell.css` and `ChatView.css` first. The `CONTRACT:` comments
beside each `background: var(--…)` rule name which selector paints
each variable and what relies on it being opaque. That's the
load-bearing knowledge — variable names without paint-site context
will lead you astray.

To verify, use `bash "$SCRIPTS_DIR/preview_shell.sh"` — it
screenshots the real authenticated chat view (not the login screen,
which doesn't mount `.shell` and gives a misleading preview).

Snapshot before overwriting — recipe and rationale live in the
skill's "Theme snapshots before overwriting" section.

## Shell layout contracts

These are the load-bearing conventions across `Shell.css` and
`ChatView.css`. Match them when adding new selectors — generic
class names break the existing CSS by sibling-selector or
contract-comment mismatch.

- **BEM naming.** Use `.chat__*`, `.shell__*`, `.drawer__*`,
  `.queued__*`. Never invent `.chat-queued` or `.shell-bar` —
  the underscore variant is what every existing rule expects.
- **Drawer scroll model.** Sections use `flex: 2 1 0` (chats),
  `flex: 1 1 0; min-height: 120px` (apps), `flex-shrink: 0`
  (settings). Never put `overflow-y` on `.drawer__body` — when
  apps grow, chats get pushed off-screen.
- **Composer is a flex pill, not an overlap.** The pill (the
  composer wrapper) is `display: flex; gap: 6px` with the
  textarea and send button as siblings. Avoid negative
  margins to "overlap" the send button onto the textarea —
  that's how text-slides-under-send-button bugs happen.
- **Pill geometry.** For a true stadium curve, `border-radius`
  equals half the height (40px tall → 20px radius), not
  `9999px`. The 9999px shortcut renders correctly for short
  pills but wrong as the pill grows.

## Shell change costs

- **theme.css only (no rebuild):** color variables, gradients, background
  images, `@keyframe` animations, Google Fonts via `@import`, pseudo-elements
  on stable class names, filters. Hot-reloaded instantly.
- **app-frame.html (no rebuild):** `/data/shell/public/app-frame.html` is
  read per-request by the backend, not compiled. Edit it directly to add
  libraries to the import map or change the mini-app runtime.
- **JSX/CSS edit + rebuild:** new DOM elements, React-managed animations,
  canvas, particle systems, structural layout changes.
  Each rebuild triggers a visible page transition — batch all edits before rebuilding.
- App **source** lives at `/data/apps/<slug>/index.jsx` (+ any
  companion files), keyed by slug. App **runtime data** written
  through the storage API lives at `/data/apps/<app_id>/<path>`,
  keyed by the numeric app id. These are SEPARATE trees — slugs and
  numeric ids don't coincide. If a mini-app PUTs to
  `/api/storage/apps/{appId}/data/foo.json`, the file lands at
  `/data/apps/<app_id>/data/foo.json` (numeric-id tree), not under
  the slug tree. Paths beginning with `data/` are gitignored if you
  want runtime files kept out of the source repo.

## Gotchas

- **Editing an existing app's JSX auto-recompiles.** A file watcher
  on `/data/apps/*/index.jsx` recompiles the bundle ~1s after you
  save. You don't need to re-run `register_app.py` just to push code
  changes to an existing app — only for the initial create (so the
  app gets an id + DB row). If your edit doesn't seem to land,
  refresh the iframe; if it still doesn't, check that
  `/data/compiled/app-<id>.js` mtime advanced.
- **`register_app.py` is for the INITIAL create only — never
  re-run it.** Edits land via the file watcher; re-registering
  creates a duplicate app whenever the `<name>` you pass differs
  by even one character from the stored display name (slug vs.
  title is the common slip — `tunnel-runner-3d` vs. `Tunnel Run
  3D`). If a duplicate appears, `DELETE /api/apps/<dup-id>`.

- **"The partner still sees the old app" — checklist, in order.**
  If you edited `/data/apps/<slug>/index.jsx` and the partner says
  it didn't change, **do not reach for `register_app.py`** — work
  these instead:
  1. **Compile mtime advanced?**
     `stat -c '%Y %n' /data/compiled/app-<id>.js` should be newer
     than your edit. If not, auto-recompile didn't fire — the
     most common cause is a JSX syntax error. Check
     `/data/logs/chat.log` for `compile failed for`.
  2. **Iframe still showing the cached bundle?** A successful
     recompile broadcasts `app_updated` which busts the iframe
     cache, but a cached iframe in the partner's drawer LRU may
     need to be reopened. Take a screenshot via
     `bash "$SCRIPTS_DIR/preview_app.sh" <id>` and Read it — if
     it shows the old UI but the file is new, the iframe is the
     stale layer.
  3. **Module-load error?** App stuck on a loading spinner means
     it's not a code-edit issue at all; check the served bundle
     and its imports — `curl -s "$API_BASE_URL/api/apps/<id>/module?token=$AGENT_TOKEN"`
     and any vendor URL it references (`/vendor/three/three.module.js`,
     `esm.sh/*`).
- **Theme revert:** `DELETE /api/storage/shared/theme.css` (no body)
  restores the platform default. Never write a partial theme.css —
  the server-injected initial-render block shadows it; either override
  completely or delete entirely. (Snapshot-before-overwrite lives in
  the skill's "Theme snapshots before overwriting" section.)
- Cron + storage API can get out of sync. Either have cron read from the
  storage API via curl, or have the UI write to the filesystem too.
- Cron scripts need `CLAUDE_CONFIG_DIR=/data/cli-auth/claude`.
- `|` inside `$...$` in markdown tables breaks both. Use `\mid` or `\vert`.
- Mini-apps get a scoped token, not the owner's full JWT. It can access
  storage, proxy, AI, notifications, push — but NOT auth, settings, or chat.
- Storage 404 on first load is normal — handle with default value.
- **Storage API asymmetry — and the envelope trap:**
  `PUT /api/storage/apps/{id}/notes.json` with body
  `{title: "hi", items: [1,2,3]}` writes
  `{"title":"hi","items":[1,2,3]}` to disk. **For `.json` paths the
  body IS the document — no envelope, no double stringify.** The
  envelope form `{content: JSON.stringify(data)}` does NOT get
  unwrapped on `.json` paths anymore; the server stores the envelope
  shape literally. The app then loads back `{content: "..."}` instead
  of its data, falls through to empty state, and the next save
  overwrites real data with empty state. Multiple apps were silently
  destroying user data this way until 2026-05-26 when the source bug
  was fixed across 18 mini-apps. **Rule of thumb: `.json` → `body:
  JSON.stringify(data)`; everything else → `body:
  JSON.stringify({content: text})`.** GET returns the raw file
  (parses cleanly with `await res.json()`); GET does not mirror PUT
  shape.
- **Floating composer:** `.chat__foot` is `position:absolute` with
  transparent background. Do NOT add background to `.chat__foot` or
  wrap its controls in a shared opaque container — that breaks the
  scroll-underneath illusion.
- **Codex auth failure mode:** when Codex turns fail with auth
  errors, surface that to the partner — don't pretend the run
  succeeded. Credentials at `/data/cli-auth/codex/` need refreshing
  via Settings → AI provider → Reconnect. (There is no
  `/codex:rescue` slash-command available inside the Möbius
  container; that exists only in the user's host Claude Code.)
- Back gesture in apps: use `pushState`/`popstate` for internal navigation.
- Three.js: `import * as THREE from 'three'` and
  `import { OrbitControls } from 'three/addons/controls/OrbitControls.js'`
  just work (self-hosted at `/vendor/three/` via the app-frame
  import map — no esm.sh waterfall).

## Screenshot helpers (instance-specific scripts)

These two helper scripts are pre-installed on this instance.
General screenshot rules (embed-before-describe, agent-browser
gotchas) live in the skill — this section only covers the
shortcuts.

For previewing a mini-app:

```bash
bash "$SCRIPTS_DIR/preview_app.sh" "$APP_ID"
```

It opens the app inside the authenticated shell (which handles the
parent-init handshake the iframe expects), sets the viewport to
match the partner's device, and writes the PNG to
`/data/chats/$CHAT_ID/generated/`. Prints the path on stdout.

For previewing the shell itself (e.g. after a theme change), use
`bash "$SCRIPTS_DIR/preview_shell.sh"`.
