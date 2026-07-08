# Theming and the shell

How to change Möbius's look and modify the shell UI: hot-reloaded `theme.css`, light/dark CSS variables, structural JSX edits that need a rebuild, lucide icons, and keeping the shell from breaking. `Read` this before any visual change to the shell.

The shell UI is fully editable. Source lives at `/data/platform/frontend/src/` (part of the `/data/platform` repo clone that actually runs).

---

## List the shell live — never trust a hardcoded file list

Hand-written file tables go stale the moment a file is renamed and send you on dead-end searches (this caused a real bug — a claimed file that no longer existed). To see what lives in the shell, or any directory on the platform:

```bash
python3 /app/scripts/describe-tree.py /data/platform/frontend/src/components/ --depth 1 --quiet
```

It prints `filename — first-sentence-of-docstring` for each file, so the listing always matches reality. Works on `/app/app/`, `/data/apps/`, mini-app folders — anything. When you write a NEW file, start it with a one-sentence docstring/top-comment — that's what describe-tree extracts (Python `"""..."""`, JSX `/* ... */`, shell leading `#`, CSS `/* ... */`).

---

## CSS-only changes — no rebuild needed

Use `/data/shared/theme.css` for visual changes — colors, fonts, gradients, animations. Hot-reloaded instantly.

```bash
curl -X PUT "$API_BASE_URL/api/storage/shared/theme.css" \
  -H "Authorization: Bearer $AGENT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"content": "<css here>"}'
bash "$SCRIPTS_DIR/notify_theme.sh"
```

**Read the current theme before modifying it:**

```bash
curl -s "$API_BASE_URL/api/storage/shared/theme.css" -H "Authorization: Bearer $AGENT_TOKEN"
```

Check `/data/shared/theme-mode` for `"light"` / `"dark"`. Make CSS work in both modes by using the standard variables (`--bg`, `--surface`, `--text`, `--accent`, `--border`, `--danger`, `--green`, `--font`, `--mono`, …) rather than hardcoded colors.

**Before overwriting `theme.css`, snapshot for a named undo.** The server auto-snapshots the prior `theme.css` to `theme.css.bak-<unix-ts>` on every overwrite, and `?reset-theme=1` (or the recovery page) rolls back a theme that breaks the UI — so a revert path always exists. Still snapshot first for your own undo:

```bash
curl -s -H "Authorization: Bearer $AGENT_TOKEN" "$API_BASE_URL/api/storage/shared/theme.css" \
  > "/tmp/theme.backup.$(date +%s).css"
```

---

## Structural changes (JSX/CSS) — a watcher rebuilds, no restart

Read source first, then save your edits under `/data/platform/frontend/src/`. A file watcher runs `vite build` into the served `dist/` on every source change (debounced, atomic swap) — there is NO manual rebuild step and NO restart. Just reload the page to see the change. Batch all edits so the watcher rebuilds once instead of on every save. For CSS-only changes, prefer `theme.css` above (hot-reloaded, no build at all). If the shell breaks, direct the partner to `/recover` → "Restore platform" (see `recovery.md`).

**If you're patching the same selector 3+ times in one chat, the component shape is probably wrong.** Extract a new component (e.g. a dedicated `ChatInputBar.jsx` for the composer) instead of stacking CSS overrides. Four failed in-place tries beats one extraction every time.

---

## Icons in the shell

`lucide-react` is in the shell's `package.json`. Import icons rather than inlining raw `<svg><path d="..."/>`:

```jsx
import { Paperclip, ArrowUp, Mic, ChevronDown, X } from 'lucide-react'
<button><ArrowUp size={20} strokeWidth={2} /></button>
```

Inline SVG path data is brittle, unreviewable in diffs, and hard to size consistently. The Lucide set covers the OpenAI Apps SDK glyphs the shell uses (Paperclip, ArrowUp/Send, Mic, ChevronDown, X, Trash, Settings, MessageSquare, Grid). Reach for inline SVG only when no equivalent exists. If `import 'lucide-react'` fails with "module not found", the container is on an older image — `cd /data/platform/frontend && npm install lucide-react` (the watcher rebuilds on the next source save).

---

## Upstream changes

When the platform is updated, shell source may change. Diff the served clone against upstream:

```bash
git -C /data/platform diff origin/main -- frontend/src | head -40
```

Pick up the changes through the platform-apply flow (rebase onto `origin/main`) rather than hand-copying files — see `contributing.md`.

---

## Protected files (read-only)

These credential-handling components cannot be modified:

- `src/components/LoginForm/LoginForm.jsx` + `.css`
- `src/components/SetupWizard/SetupWizard.jsx` + `.css`
- `src/components/ProviderAuth/ProviderAuth.jsx` + `.css`

Only files listed in `/app/protected-files.txt` are root-owned (chmod 444/555). Everything else in the shell is mobius-owned and editable.

---

## Protecting the shell from breaking

The chat is the partner's only way to reach you. Be careful that shell edits don't break navigation, delete chats, or remove the input area. Before rebuilding, review changes:

```bash
cd /data && git diff -- shell/
```

If the shell breaks, direct the partner to `/recover` → "Restore shell" (see `recovery.md`). After substantial shell work, `pm-commit 'shell: <what, why>'` so the change is in `/data`'s git history.
