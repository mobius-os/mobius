# Experience

This is your instance. The apps you build live here, the knowledge
you log here persists, and every session you improve this file makes
the next session smarter. Treat it as your own project notes.

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

**What goes in:**
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

**After delivering a completed app**, ask the partner: does this
look right? Is anything missing or worth changing? Don't close the
loop unilaterally — the partner's feedback is how you get better.

**Before building anything non-trivial**, ask the partner 2–3
clarifying questions: preferred mood/theme, must-have vs nice-to-have
features, any specific patterns they care about. One message, not a
long back-and-forth. This is scoping, not adding features — it
narrows the build to what the partner actually wants, which is the
opposite of over-building.

**While building**, share screenshots inline as you take them — don't
save them all for the end. Caption each one for the partner, not for
yourself: describe what's visible and whether it looks right ("the
mood grid is rendering with color-coded cells — the emoji picker
works but the calendar header needs fixing"). The partner sees your
messages as you write them and wants to follow along in real time,
not read a debug log after the fact.

## Experience log

Add new entries at the bottom. Delete outdated ones. No timestamps;
order is implicit.

- Built **Hello World**. A welcome screen with an "ask the agent"
  button that takes the user to chat. The simplest possible starting
  point — proves the app contract works and gives the user somewhere
  to click.

## Platform state

- Shell source: `/data/shell/src/` — editable JSX/CSS/components
- Shell build: `/data/shell/dist/` — Vite output, overrides `/app/static/`
- Read-only originals: `/app/shell-src/`
- Rebuild command: `bash /app/scripts/rebuild_shell.sh`
- Theme (CSS-only, no rebuild): `/data/shared/theme.css`
- Theme mode (`"light"` or `"dark"`): `/data/shared/theme-mode`
- Notify after theme change: `bash "$SCRIPTS_DIR/notify_theme.sh"`

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

## CSS classes safe to target for theming

`.sidenav`, `.sidenav__item`, `.drawer`, `.drawer__item`, `.chat__text`,
`.chat__text--user`, `.chat__text--assistant`, `.chat__form`, `.chat__input`,
`.md-blocks`, `.md-paragraph`, `.md-code-block`, `.md-heading`.

## Before building: check what exists

```bash
curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/apps/" | python3 -m json.tool
```

## Design principles

- **Partner-facing language by default.** Describe what the app does
  and how it feels, not how it's built. Say "your data saves across
  sessions" not "persisted via Storage API." Only include
  implementation details (technologies, APIs, architecture) when the
  partner explicitly asks or when reporting an error that requires
  technical context.
- CSS variables (`var(--bg)`, `var(--accent)`, etc.) — never hardcode colors
- Check `/data/shared/theme-mode` to know light vs dark mode
- Typography: choose fonts that match the mood, Google Fonts via `@import`
- Color: cohesive palette using the existing CSS variables as a base
- Motion: subtle CSS transitions for hover and state changes
- Spatial: generous negative space, consistent padding
- Mobile-first: the partner's viewport size is in the session context

## Reusable patterns from the shell

| Component | Path | Purpose |
|-----------|------|---------|
| `ChatInput` | `ChatView/ChatInput.jsx` | Text input with voice, file attach, send/stop |
| `BlockRenderer` | `ChatView/markdown/BlockRenderer.jsx` | Streaming markdown renderer |
| `InlineContent` | `ChatView/markdown/InlineContent.jsx` | Inline markdown (links, images, math) |
| `ImageLightbox` | `ChatView/markdown/ImageLightbox.jsx` | Pinch-zoom image viewer |

These can't be imported directly (different bundle) — use them as
reference implementations only.

## Shell change costs

- **theme.css only (no rebuild):** color variables, gradients, background
  images, `@keyframe` animations, Google Fonts via `@import`, CSS filters,
  pseudo-elements on stable class names, `backdrop-filter`. Hot-reloaded instantly.
- **app-frame.html (no rebuild):** `/data/shell/public/app-frame.html` is
  read per-request by the backend, not compiled. Edit it directly to add
  libraries to the import map or change the mini-app runtime.
- **JSX/CSS edit + rebuild:** new DOM elements, React-managed animations,
  canvas, particle systems, structural layout changes.
  Each rebuild triggers a visible page transition — batch all edits before rebuilding.

## Gotchas

- Cron + storage API can get out of sync. Either have cron read from the
  storage API via curl, or have the UI write to the filesystem too.
- Cron scripts need `CLAUDE_CONFIG_DIR=/data/cli-auth/claude`.
- `|` inside `$...$` in markdown tables breaks both. Use `\mid` or `\vert`.
- Mini-apps get a scoped token, not the owner's full JWT. It can access
  storage, proxy, AI, notifications, push — but NOT auth, settings, or chat.
- Empty JSX = silent compile failure (0-byte output, "no default export").
  Always verify: `wc -l apps/<name>/index.jsx`.
- Storage 404 on first load is normal — handle with default value.
- Back gesture in apps: use `pushState`/`popstate` for internal navigation.
- Async assets (Three.js textures, images, lazy components, fonts)
  aren't rendered at the moment `agent-browser open` returns —
  screenshots captured immediately come back empty or half-loaded.
  Wait for them: `agent-browser open "$URL" && agent-browser wait 8000 && agent-browser screenshot "$OUT"`.
  Tune the wait to match the heaviest asset.
- Proxy endpoint is `/api/proxy?url=<urlencoded>` — the external URL
  MUST be URL-encoded. Building the call inline:
  `curl -s "$API_BASE_URL/api/proxy?url=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1],safe=""))' "https://example.com/api")"`.
  Raw unencoded URLs will be routed wrong or rejected.
- **Mini-apps must forward their scoped token on proxy calls.** The
  iframe passes `token` as a prop to the default-exported component.
  Inside the app, include it on every proxy request:
  `fetch(\`/api/proxy?url=\${encodeURIComponent(url)}\`, {
    headers: { Authorization: \`Bearer \${token}\` }
  })`. Without the header the proxy returns silently empty data and
  the app looks broken for no visible reason.
- Canvas ref trap: a `ref={el => { canvas = el; scale(dpr) }}` callback
  runs on every React render, so every re-render re-applies the DPR
  scale and the drawing gets progressively distorted. Guard with a
  `useCallback`, a `useRef`-based "already initialized" flag, or an
  explicit `resetTransform()` before each setup.

## Debug endpoints

- Active agents: `curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/debug/status"`
- Chat logs: `curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/debug/logs?lines=50"`
- Filter by chat: add `&chat_id=<id>` to the logs endpoint.

## Screenshots and interactive testing with agent-browser

`agent-browser` is a CLI for a headless Chromium, installed globally
in the container. Session-based: open a URL once, then take screenshots,
click, fill, etc. against that session.

### Basic screenshot

```bash
APP_ID=<id>
APP_URL="$API_BASE_URL/api/apps/$APP_ID/frame?token=$AGENT_TOKEN&v=$(date +%s)"
OUT="/data/chats/$CHAT_ID/generated/app-$APP_ID-preview.png"
mkdir -p "$(dirname "$OUT")"

# Viewport W and H come from the `Viewport: WxH` line in the session
# context above. Substitute those exact values — do NOT invent a default.
agent-browser set viewport <W> <H>
agent-browser open "$APP_URL"
agent-browser screenshot "$OUT"
agent-browser close
```

- The partner's viewport is in the session context as `Viewport: WxH`.
  If that line is missing, ask the partner instead of guessing — a
  wrong viewport silently produces misleading screenshots.
- Embed the PNG inline in the next chat message:
  `![preview](/api/chats/<chat_id>/generated/app-<id>-preview.png)`

### Interactive testing

The session stays open between commands until `close` is called, so
interactions (click, fill, drag, scroll) can be sequenced.

```bash
agent-browser set viewport <W> <H>    # from `Viewport: WxH` in context
agent-browser open "$APP_URL"
agent-browser snapshot                     # elements with @eN refs (text)
agent-browser click @e5                    # click "Add Task"
agent-browser fill @e8 "Buy groceries"     # clear + type into a field
agent-browser press Enter                  # submit
agent-browser screenshot /data/chats/$CHAT_ID/generated/after-click.png
agent-browser close
```

Common commands: `click <sel>`, `fill <sel> <text>`, `type <sel> <text>`,
`press <key>`, `drag <src> <dst>`, `scroll <dir>`, `wait <sel|ms>`,
`get text <sel>`, `eval <js>`.

Use interactive testing to verify interactions (drag-and-drop,
form submission, navigation) before claiming an app is done.

## Notifications

```bash
curl -s -X POST -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"title":"App ready","body":"<name> is built and ready to use"}' \
  "$API_BASE_URL/api/notifications/send"
```

Send a notification after finishing or updating an app so the partner
knows even if they've switched tabs.
