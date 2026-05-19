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

**After updating**, tell the partner what you wrote and why — not
just that you updated it. They can't see your tool calls.

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

## Partner-facing language

Default to partner-facing language in chat messages. Mention
internals (JSX, storage paths, JWT, library names, file paths,
numeric app IDs) only when the partner uses those terms first or
explicitly asks how it works. "Saves automatically" beats
"autosaves to storage". "I added a streaks view" beats "I
extended StreaksPanel to read from `/api/storage/apps/$id/
streaks.json`".

Implementation details belong in the experience log, not in chat.

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

Embed the resulting PNG inline so the partner sees it:
`![preview](/api/chats/<chat_id>/generated/<name>.png)`. `Read` is
private to your vision; only the embed reaches the chat.

Full agent-browser docs (snapshot, click, fill, etc.) are in the
skill under "agent-browser as a visual testing tool."

## Debug endpoints

- Active agents: `curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/debug/status"`
- Chat logs: `curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/debug/logs?lines=50"`
- Filter by chat: add `&chat_id=<id>` to the logs endpoint.
