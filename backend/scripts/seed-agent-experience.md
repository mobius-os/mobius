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

## Opening a new chat

Möbius redefined a bit on 2026-05-30: the walkthrough that fires on
first sign-in now positions Möbius explicitly as "a chat surface in
front of a coding agent with write access to the platform" — theme,
shell UI, drawer, apps, even how you talk to them. The new owner has
just seen that framing.

Match it on the first message of a fresh chat. Specifically:

- **Don't introduce yourself** ("Hi, I'm Möbius's agent and I can…").
  The partner just read what you can do. Restating it sounds bot-y.
- **Don't end with "How can I help you today?"** — generic
  AI-assistant tropes signal a regression from the walkthrough's
  framing. Pick a posture instead.
- **For a low-signal opener** ("hi", "what's up", "test"): respond
  with 2–3 concrete starting points the owner could try right now,
  one line each. Examples:
  - "Swap to a light theme."
  - "Add a habit tracker mini-app to the drawer."
  - "Hide the voice button from the composer."
  Keep it short — a tasting menu, not a brochure.
- **For an obvious build request**: skip the chat and start
  building (existing "triage the prompt" rules below still apply).
- **For an underspecified build request**: confidence-default —
  build a v1 with the 2–3 most useful choices made, and offer
  redirects in the same message. The point is to move; the partner
  can pivot off a real artifact.
- **For "what can you do" questions explicitly**: answer with
  capability categories ("change the theme, build mini-apps,
  modify the shell, schedule cron jobs, recover from breakage")
  not a feature list. Three sentences max.

The walkthrough also tells the partner that recovery lives at
`/recover` and `/recover/chat`. If they ever ask "what happens if
something breaks" or similar — point at those, not at a generic
"you can ask me to fix it." The recovery surface runs on its own
boot stack and stays reachable when the main chat doesn't.

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

**If you call AskUserQuestion the runner pauses until the partner
answers.** chat.py freezes the turn at the question event — there
is no auto-build on dismissal. Don't fire a question if you'd
rather just build; pick defaults and ship. Only ask when you
genuinely cannot pick a sensible default.

Everything below — register_app.py, screenshots, notifications —
runs *after* you've decided to build, not instead of deciding.

## Before doing something destructive to the partner's data

I've learned to stop and ask before deleting, overwriting,
truncating, resetting, migrating, or bulk-editing anything the
partner might own. Confidence in reversibility isn't consent. When
ownership is unclear, ask — one wrong destructive call matters more
than the friction. The exception is data I created myself this turn
as a disposable test fixture; cleaning up after that is fine.

## Git

`/data/` is a git repo, initialized on first boot. After important
or substantial changes, commit so undo is clean:
`pm-commit 'one-line what and why'` — wrapper that stages, unstages
a runtime-state denylist (`agent-browser-profiles/`, `compiled/`,
`logs/`, `cron-logs/`, `push/`, `generated/`), then commits. If
more than 50 files are staged after the denylist filter, it refuses
with exit 2 and unstages everything (so you're not left holding a
wedged index). Re-run as `pm-commit --allow-broad 'msg'` after
confirming the staged set is actually what you meant to commit.

## Experience log

Add new entries at the bottom. Delete outdated ones. No timestamps;
order is implicit.

- Auto-installed **App Store** on first boot (slug `store`, from
  the curated manifest at `BOOTSTRAP_STORE_MANIFEST_URL`). The
  store is the user's first surface for discovering installable
  mini-apps and proves the install endpoint works end-to-end. If
  the bootstrap install failed (offline at first boot, GitHub
  blip), it will retry next container restart — the user can also
  manually install via `POST /api/apps/install` with the manifest
  URL. The earlier "Hello World" seed-on-first-boot was retired
  on 2026-05-30 in favor of the store as the more useful starting
  point.

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
  `theme.py:DEFAULT_THEME` — `grep -r 'var(--' /data/shell/src/` for
  live usage. Don't invent fallbacks like `var(--fg, #111)` — there
  is no `--fg`, and a near-black fallback is invisible on dark mode.
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

Snapshot `theme.css` before overwriting — the skill's
ensure-checklist row prescribes the `curl` recipe.

## Shell layout contracts

These are the load-bearing conventions across `Shell.css` and
`ChatView.css`. Match them when adding new selectors — generic
class names break the existing CSS by sibling-selector or
contract-comment mismatch.

- **BEM naming.** Use `.chat__*`, `.shell__*`, `.drawer__*`,
  `.queued__*`. Never invent `.chat-queued` or `.shell-bar` —
  the underscore variant is what every existing rule expects.
- **Drawer scroll model.** Single `.drawer__scroll-wrap` holds
  New chat + Chats + Apps. Both sections use `flex: 1 1 0;
  min-height: 80px` — symmetric, each scrolls internally.
  Settings row sits in `.drawer__group--bottom` outside the
  scroll-wrap, pinned to the drawer bottom. Never add
  `overflow-y` to `.drawer__body` — sections scroll internally
  via `.drawer__scroll`, and adding overflow to the body breaks
  that model and makes sections race for space.
- **Composer is a flex pill, not an overlap.** The composer is a
  flex row: `.composer-plus` (the `+` button) and `.chat__pill`
  are siblings in `.chat__form`. The pill contains the textarea,
  send/stop, and mic. Avoid negative margins to "overlap" the
  send button onto the textarea — that's how
  text-slides-under-send-button bugs happen.
- **Pill geometry.** For a true stadium curve, `border-radius`
  equals half the height (48px tall → 24px radius), not
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
- I've burned myself re-running `register_app.py` on an existing
  app — it creates a duplicate every time the name differs by a
  character (slug vs. title is the common slip — `tunnel-runner-3d`
  vs. `Tunnel Run 3D`). Edits land via the file watcher; only run
  `register_app.py` for the initial create. If a duplicate appears,
  `DELETE /api/apps/<dup-id>`.

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
     `bash "$SCRIPTS_DIR/preview_app.sh" <id>` and view it — if
     it shows the old UI but the file is new, the iframe is the
     stale layer.
  3. **Module-load error?** App stuck on a loading spinner means
     it's not a code-edit issue at all; check the served bundle
     and its imports — `curl -s "$API_BASE_URL/api/apps/<id>/module?token=$AGENT_TOKEN"`
     and any vendor URL it references (`/vendor/three/three.module.js`,
     `esm.sh/*`).
- **Theme revert:** `DELETE /api/storage/shared/theme.css` (no body)
  restores the platform default. Never write a partial theme.css —
  the server-injected initial-render block shadows it; either
  override completely or delete entirely. (Snapshot-before-overwrite
  lives in the skill's ensure-checklist.)
- **Shell rebuild doesn't live-reload.** After `bash
  $SCRIPTS_DIR/rebuild_shell.sh`, the running uvicorn still serves
  the old bundle until the process restarts. Tell the partner the
  shell will update after the next container restart.
- **Cron entries don't survive container rebuilds.** `/var/spool/cron/`
  lives in the image layer, not on `/data`, so any rebuild
  (`docker compose build && up`, Railway/Fly/Render redeploy, image
  pull) starts with an empty crontab. The entrypoint replays
  `/data/apps/*/init-cron.sh` on boot to put entries back, so every
  cron task needs a matching `init-cron.sh` — without it, the
  schedule silently vanishes on the next rebuild. Use the scaffold:
  `bash /app/scripts/init-cron-scaffold.sh <slug> "<schedule>"`. It
  writes `job.sh` + `init-cron.sh` + installs the live entry,
  idempotent. Never call `crontab -u mobius` directly without
  writing the matching `init-cron.sh`.
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
  `{"title":"hi","items":[1,2,3]}` to disk. For `.json` paths the
  body IS the document — no envelope, no double stringify. The
  envelope form `{content: JSON.stringify(data)}` does not get
  unwrapped on `.json` paths; the server stores the envelope shape
  literally, the app loads back `{content: "..."}` instead of its
  data, falls through to empty state, and the next save overwrites
  real data with empty state. Rule of thumb: `.json` → `body:
  JSON.stringify(data)`; everything else → `body:
  JSON.stringify({content: text})`. GET returns the raw file (parses
  cleanly with `await res.json()`); GET does not mirror PUT shape.
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
