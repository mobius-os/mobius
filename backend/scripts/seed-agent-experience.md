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

**If the AskUserQuestion tool / question card returns without an
answer** (dismissed, errored, partner skipped) — do NOT retry.
Pick the recommended defaults, build, and let the partner redirect
after. Hesitating wastes turns; ambiguous responses to a tool the
partner doesn't see as blocking are not signal.

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

## Speaking to the partner

The partner's mental model should contain only entities that
affect their experience. Agent infrastructure — your tool calls,
your file paths, your internal IDs, your verification mechanisms —
exists so you can do the work; it doesn't belong in the partner's
model. (A high-level plan for what you're about to do is
different — that's direction the partner can follow and redirect.
The mechanism beneath it isn't. The *content* of what you note in
your memory is also fair game when it's partner-relevant — what
isn't is the mechanism of the noting.)

The partner sets the register. If they use technical vocabulary,
match them; descend further than they ask for and the
conversation feels like documentation. Stay above the partner's
register and you sound vague; that's better than below.

**Group-level plans, not per-tool announcements.** Before a batch
of related tool calls, give the partner a one-sentence high-level
description of what the next chunk of work accomplishes — at their
altitude, not the tool's. Inside the batch, individual tools run
silently; they're covered by the phase you announced. New phase
gets a new sentence. The failure mode is announcing each tool when
it's already inside a phase you already framed.

Specific patterns the infrastructure principle also rules out:

- **Identifiers and paths.** Internal IDs ("id 2", `/app/4`), file
  paths, the names of files you wrote to. When pointing at a
  built app, say "Open it from the drawer" or use the
  partner-facing name.
- **Debugging narration — past tense counts too.** "React error
  #31, the import-map needs `?external=react`" is infrastructure
  whether you write it while debugging or afterward as
  "fixed and noted that the markdown library was shipping its
  own React copy." If the partner asks how a failure was fixed,
  match their register; otherwise the mechanism stays out of the
  chat. The partner's problem is "the previewer crashed"; the
  rest is your problem to solve.

## Experience log

Add new entries at the bottom. Delete outdated ones. No timestamps;
order is implicit.

- Built **Hello World**. A welcome screen with an "ask the agent"
  button that takes the user to chat. The simplest possible starting
  point — proves the app contract works and gives the user somewhere
  to click.

## Shell structure

| File | Controls |
|------|---------|
| `Shell/Shell.jsx` | Logo bar, drawer toggle, layout, system events |
| `Shell/Shell.css` | Logo bar and layout styles |
| `ChatView/ChatView.jsx` | Chat messages, streaming, scroll |
| `ChatView/ChatView.css` | Chat styles |
| `ChatView/ChatInput.jsx` | Chat input, voice, file upload, send/stop |
| `ChatView/ChatInput.css` | Input styles |
| `Drawer/Drawer.jsx` | Side drawer, chat list, app list |
| `Drawer/Drawer.css` | Drawer styles |
| `AppCanvas/AppCanvas.jsx` | Mini-app iframe |
| `index.css` | Global CSS variables and resets |

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

## Gotchas

- **Editing an existing app's JSX auto-recompiles.** A file watcher
  on `/data/apps/*/index.jsx` recompiles the bundle ~1s after you
  save. You don't need to re-run `register_app.py` just to push code
  changes to an existing app — only for the initial create (so the
  app gets an id + DB row). If your edit doesn't seem to land,
  refresh the iframe; if it still doesn't, check that
  `/data/compiled/app-<id>.js` mtime advanced.
- **Reverting the theme:** `DELETE /api/storage/shared/theme.css` (no
  body needed). The platform owns defaults — `/api/theme` returns the
  user override if present, otherwise the built-in default. Don't try
  to write a "minimal" theme.css with only a few variables; it gets
  shadowed by the server-injected initial-render block. Either
  override completely OR delete entirely.
- Cron + storage API can get out of sync. Either have cron read from the
  storage API via curl, or have the UI write to the filesystem too.
- Cron scripts need `CLAUDE_CONFIG_DIR=/data/cli-auth/claude`.
- `|` inside `$...$` in markdown tables breaks both. Use `\mid` or `\vert`.
- Mini-apps get a scoped token, not the owner's full JWT. It can access
  storage, proxy, AI, notifications, push — but NOT auth, settings, or chat.
- Storage 404 on first load is normal — handle with default value.
- **Storage API read shape is asymmetric**: `PUT` takes
  `{content: JSON.stringify(myData)}`; `GET` returns the parsed
  inner object directly, NOT an envelope. Past agents have lost
  rebuild cycles assuming GET mirrors PUT. (See the skill for the
  full API examples.)
- Back gesture in apps: use `pushState`/`popstate` for internal navigation.
- Three.js: `import * as THREE from 'three'` and
  `import { OrbitControls } from 'three/addons/controls/OrbitControls.js'`
  just work (self-hosted at `/vendor/three/` via the app-frame
  import map — no esm.sh waterfall).

## Screenshots — quick start

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

**Embed-before-describe is a syntactic rule, not a vibe.** When you
`Read` a PNG, the next text block you write must contain
`![caption](/api/chats/<chat_id>/generated/<name>.png)` before any
prose that mentions what the screenshot shows. Embed and description
live in the same text block, embed first. This is a check you can
run on yourself: before sending a text block that mentions a
screenshot, confirm the string `![` and the file path are present in
that block. "Share inline" without this check reads as "share
eventually" and you end up collating the embeds into a final summary
— that defeats the point, because the partner was following along
through your running narrative. `Read` is private to your vision;
only the `![]` embed reaches the chat.

Three patterns that come up every session when driving agent-browser
directly (click, fill, etc.):

- **Scale `wait` to the heaviest asset.** Three.js textures / WebGL /
  large fonts → 6000–8000ms; ordinary React apps → 1000–1500ms;
  static HTML → 200ms. Blanket-8000 everywhere wastes session time.
- **Re-snapshot after every DOM-mutating action.** `@eN` refs from
  `agent-browser snapshot` are invalidated by any click, navigate,
  or re-render. After action, snapshot again before targeting `@ref`.
- **`✓ Done` confirms dispatch, not state change.** The CLI returns
  Done the instant the command is sent to Chromium, not after the
  UI transitions. Verify with another snapshot or a screenshot
  after any click that's supposed to change state.

Full agent-browser docs are in the skill under "agent-browser as a
visual testing tool."

## Debug endpoints

- Active agents: `curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/debug/status"`
- Chat logs: `curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/debug/logs?lines=50"`
- Filter by chat: add `&chat_id=<id>` to the logs endpoint.
