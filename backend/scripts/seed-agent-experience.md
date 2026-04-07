# Agent experience

Accumulated knowledge from working in this Möbius instance. Read at the
start of every session. Update when you learn something future sessions
should know. Keep it concise — this is injected into every session prompt.

## Platform state

- Shell source: `/data/shell/src/` — editable JSX/CSS/components
- Shell build: `/data/shell/dist/` — Vite output, overrides `/app/static/`
- Read-only originals: `/app/shell-src/`
- Rebuild command: `bash /app/scripts/rebuild_shell.sh`
- Theme (CSS-only, no rebuild): `/data/shared/theme.css`
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

## Stable CSS class names for theme targeting

`.sidenav`, `.sidenav__item`, `.drawer`, `.drawer__item`, `.chat__text`,
`.chat__text--user`, `.chat__text--assistant`, `.chat__form`, `.chat__input`,
`.md-blocks`, `.md-paragraph`, `.md-code-block`, `.md-heading`.

## Listing existing apps

```bash
curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/apps/" | python3 -m json.tool
```

Check this before building something that might already exist.

## PWA icon regeneration

When changing `--bg` in theme.css, icons need regenerating (they embed the
background color). Recipe:

```bash
python3 -c "
import re, os
from PIL import Image
from pathlib import Path
css = Path('/data/shared/theme.css').read_text() if Path('/data/shared/theme.css').exists() else ''
m = re.search(r'--bg:\s*(#[0-9a-fA-F]{3,8})', css)
bg = m.group(1) if m else '#0c0f14'
r, g, b = int(bg[1:3],16), int(bg[3:5],16), int(bg[5:7],16)
src = Image.open('/app/shell-src/public/moebius.png')
sz, pad = src.size[0], int(src.size[0] * 1.25)
static = '/data/shell/dist' if os.path.isdir('/data/shell/dist') else '/app/static'
for out, path in [(192, f'{static}/icons/icon-192.png'),
                  (512, f'{static}/icons/icon-512.png'),
                  (180, f'{static}/apple-touch-icon.png')]:
    c = Image.new('RGBA', (pad, pad), (r, g, b, 255))
    c.paste(src, ((pad-sz)//2, (pad-sz)//2), src)
    c.resize((out, out), Image.LANCZOS).save(path)
print(f'Icons regenerated with {bg}')
"
```

## Design principles

- Typography: choose fonts that match the mood, use Google Fonts via @import
- Color: cohesive palette, dominant colors with sharp accents
- Motion: subtle CSS transitions for hover and state changes
- Spatial: generous negative space, consistent padding
- Backgrounds: gradients and layered transparencies, not flat solids

## Reusable components for mini-apps

The shell includes components that mini-apps can reference as patterns:

| Component | Path | Purpose |
|-----------|------|---------|
| `ChatInput` | `ChatView/ChatInput.jsx` | Text input with voice, file attach, send/stop |
| `BlockRenderer` | `ChatView/markdown/BlockRenderer.jsx` | Streaming markdown renderer |
| `InlineContent` | `ChatView/markdown/InlineContent.jsx` | Inline markdown (links, images, math) |
| `ImageLightbox` | `ChatView/markdown/ImageLightbox.jsx` | Pinch-zoom image viewer |

These are in `/data/shell/src/components/`. Mini-apps can't import them
directly (different bundle), but use them as reference implementations.
If a mini-app needs a chat interface, copy the patterns from ChatInput
and BlockRenderer rather than writing from scratch.

## Apps built

(none yet)

## Shell change costs

- **theme.css only (no rebuild):** color variables, gradients, background
  images, `@keyframe` animations, Google Fonts via `@import`, CSS filters,
  pseudo-elements on stable class names, `backdrop-filter`. Hot-reloaded instantly.
- **JSX/CSS edit + rebuild:** new DOM elements, React-managed animations,
  canvas, particle systems, falling/floating elements, structural layout changes.
  Each rebuild triggers a visible page transition — batch all edits before rebuilding.

## User preferences

- UI dynamism: unknown — ask on first theme request, then keep this current as preferences evolve

## Known gotchas

- When an app has both a cron script (reads from filesystem) and a UI
  settings tab (reads/writes via storage API), the two can get out of
  sync. Either have the cron script read from the storage API via curl,
  or have the UI write to the filesystem path too.
- Cron scripts that call the `claude` CLI must set
  `CLAUDE_CONFIG_DIR=/data/cli-auth/claude` — cron runs in a clean environment
  and won't find credentials at the default `~/.claude/` path.
- **Math inside markdown tables:** the chat renders markdown first, then
  KaTeX. A `|` inside `$...$` in a table cell gets interpreted as a
  column separator before KaTeX sees it, breaking both the table and the
  math. Use `\mid` (conditionals) or `\vert` (norms) instead of `|`
  when writing math inside table cells.
- Mini-apps receive a scoped token, not the owner's full JWT. The scoped
  token can access storage, proxy, AI, notifications, and push — but NOT
  auth, settings, or chat endpoints.
