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

**Before overwriting `theme.css`, snapshot for a named undo.** The server auto-snapshots the prior `theme.css` to `theme.css.bak-<unix-ts>` on every overwrite, and `?reset-theme=1` (or the recovery page) rolls back a theme that breaks the UI — so a revert path always exists. Still snapshot first for your own undo, and keep that backup under persistent `/data/shared`; `/tmp` is cleared on restart:

```bash
theme_backup="/data/shared/theme.css.bak-agent-$(date +%s)"
cp -- /data/shared/theme.css "$theme_backup"
```

To return to the built-in theme, use `POST /api/theme/reset`, `/?reset-theme=1`, or the recovery page. Do not treat truncating `theme.css` as a complete visual reset: the current document can retain injected variables or an inline body background until the supported reset path removes the override and the shell reloads.

Keep experimental overlays bounded and cheap. Full-viewport animated gradients and blend modes can obscure content or consume substantial CPU even when a screenshot looks fine. Exercise animation, scrolling, and hover behavior for 10–15 seconds, and provide a `prefers-reduced-motion` fallback for every non-essential animation.

---

## Structural changes (JSX/CSS) — a watcher rebuilds, no restart

Read source first, then save your edits under `/data/platform/frontend/src/`. A file watcher runs `vite build` into the served `dist/` on every source change (debounced, atomic swap) — there is NO manual rebuild step and NO restart. Just reload the page to see the change. Batch all edits so the watcher rebuilds once instead of on every save. For CSS-only changes, prefer `theme.css` above (hot-reloaded, no build at all). If the shell breaks, direct the partner to `/recover` → "Restore platform" (see `recovery.md`).

After finishing a burst of shell edits, wait for the watcher build to land before POSTing `{"type":"shell_apply_now"}` to `/api/notify` with the same authenticated call shape as `notify_theme.sh`. The watcher builds within a few seconds of the last save; the rebuild events ride the shell's own system event stream (not the chat), so you won't see them here — give the build a few seconds, then confirm the build actually carried your change: `grep` the served bundle (`/data/platform/frontend/dist/assets/index-*.js`) for a distinctive string you just added. A fresh `dist/` mtime alone can mislead (an incremental/cached build can rewrite the file without your change), which is how a "rebuilt" shell can still serve the old code — grep for the change, don't trust the timestamp.

After a git/platform update, not a normal save, the watcher sees no edit event; kick it explicitly by touching a changed file under `/data/platform/frontend/src`, then restart if prompted. The updater does not auto-detect frontend changes by design, so run the step explicitly after frontend-touching platform updates.

**Known ceiling — the Android system gesture/nav bar color.** In the installed PWA on Android, the OS draws the bottom gesture/nav bar and you cannot make it exactly match `--bg`: `<meta name="theme-color">` and `viewport-fit=cover` are hints the OS may honor partially, not controls. Set `theme-color` to the theme bg as a best effort, but don't chase an exact match past one attempt — the only stronger lever is fullscreen (which hides the bar entirely). Tell the partner it's an OS-owned surface rather than iterating on it.

**If you're patching the same selector 3+ times in one chat, the component shape is probably wrong.** Extract a new component (e.g. a dedicated `ChatInputBar.jsx` for the composer) instead of stacking CSS overrides. Four failed in-place tries beats one extraction every time.

---

## Icons in the shell

`lucide-react` is in the shell's `package.json`. Import icons rather than inlining raw `<svg><path d="..."/>`:

```jsx
import { Paperclip, ArrowUp, Mic, ChevronDown, X } from 'lucide-react'
<button><ArrowUp size={20} strokeWidth={2} /></button>
```

Inline SVG path data is brittle, unreviewable in diffs, and hard to size consistently. The Lucide set covers the OpenAI Apps SDK glyphs the shell uses (Paperclip, ArrowUp/Send, Mic, ChevronDown, X, Trash, Settings, MessageSquare, Grid). Reach for inline SVG only when no equivalent exists. Dependency additions belong in the repo/image, not as runtime installs.

---

## Upstream changes

When the platform is updated, shell source may change. Inspect the served clone:

```bash
cd /data/platform
git status --short frontend/src frontend/public
git diff -- frontend/src frontend/public | head -80
touch /data/platform/frontend/src/path/to/changed-file.jsx
```

Pick up the changes through the platform-apply flow (rebase onto `origin/main`) rather than hand-copying files — see `contributing.md`.

---

## Protected files (read-only)

Only files listed in `/app/protected-files.txt` are root-owned (chmod 444/555).
Everything else in the served platform frontend is mobius-owned and editable.

---

## Protecting the shell from breaking

The chat is the partner's only way to reach you. Be careful that shell edits don't break navigation, delete chats, or remove the input area. Before rebuilding, review changes:

```bash
cd /data/platform && git diff -- frontend/
```

If the shell breaks, direct the partner to `/recover/chat`; a fresh agent can
fix `/data/platform/frontend` or restore the platform clone (see `recovery.md`).
After substantial shell work, commit it in `/data/platform`.
