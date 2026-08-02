# Visual testing and screenshots

Visual-testing extension for Möbius shell and app work. Read it alongside
`building-apps-quickstart.md` for every mini-app build/update, or alongside
`theming.md` for shell UI changes; it owns browser interaction, screenshots,
and visible evidence.

## Drive the rendered page with agent-browser

`agent-browser` is a CLI wrapping a headless Chromium with a persistent session — your visual testing tool. Seeing the app as it renders beats trusting the code for anything visual.

**To screenshot any Möbius page, use the authenticated helper — never `agent-browser open` it directly.** Your browser starts with an empty `localStorage`, so opening a Möbius URL lands on the login wall and every screenshot is the password form, not the page you meant to capture. The helper writes your scoped token into `localStorage` first, then navigates:

```bash
bash "$SCRIPTS_DIR/agent-screenshot.sh" <route> <out.png>
# /                → the shell      /chat/<id>     → a chat
# /app/<id>        → a mini-app in the shell (numeric id)
# /apps/<slug>/    → a mini-app's standalone PWA page (by slug)
```

`preview_app.sh <id>` and `preview_shell.sh [chat_id]` are thin wrappers over it
for those two common cases. `preview_app.sh` is readiness-gated and uses
ephemeral content-only mode: it waits for the real post-render frame-mounted
state and prevents product-owned walkthrough/install overlays from mounting in
that isolated browser session without writing onboarding or dismissal state. Use the helper, then
`Read`/`view_image` the PNG before describing it.

Raw `agent-browser open <url>` is for **non-Möbius pages only** (an external site you're scraping or sanity-checking) — it has no auth dance, so it shows the login wall for any Möbius route.

After changing state or temporarily injecting CSS into an already-open Möbius
page, capture the result through the same verified boundary without navigating:

```bash
bash "$SCRIPTS_DIR/agent-screenshot.sh" --current-page <route> <out.png>
```

Do **not** fall back to raw `agent-browser screenshot` for a Möbius comparison;
that bypasses the real display density, exact-font readiness, freshness, and
atomic-output checks. Raw capture remains appropriate for non-Möbius pages.

Core moves once a page is open: `set viewport "$VIEWPORT_WIDTH" "$VIEWPORT_HEIGHT" "$VIEWPORT_PIXEL_RATIO"` (the helper sets the complete geometry for you; needed when driving raw non-Möbius pages), `snapshot` (a11y tree with `@eN` refs), `click/fill/type @eN`, `wait` (on a signal — `wait @eN` / `--text` / `--fn` / `--url` — not a guessed duration), `batch "cmd1" "cmd2"` (ordered, fewer round-trips), `diff snapshot` / `diff screenshot --baseline <before>.png`.

**Write a viewable image once, at its final home.** Any screenshot, render,
crop, montage, or other raster image you intend to pass to `Read`/`view_image`
must be created directly under `/data/chats/$CHAT_ID/media/`, not created in
`/tmp` and copied afterward. The chat shell deliberately cannot serve
arbitrary `/tmp` files. Mint a unique final path before the producing command:

```bash
MEDIA_DIR="/data/chats/$CHAT_ID/media"
mkdir -p "$MEDIA_DIR"
OUT="$MEDIA_DIR/inspect-$(date +%s%N).png"
bash "$SCRIPTS_DIR/agent-screenshot.sh" --current-page <route> "$OUT"
```

`/tmp` remains correct for logs, diffs, test workspaces, disposable browser
warm-ups, and images that will never be inspected or shown. When an external
tool controls its own output location, publish that unavoidable pre-existing
file once with `publish_chat_image.py`; do not make `/tmp` the default for
images you create yourself.

For textboxes, use `fill @eN "value"` directly. Do not split that into
`click`, `Control+A`, and a selector-less `type`; the extra commands add
round-trips and the final `type` has no target. Batch independent operations
with stable selectors, then take one verification snapshot. A sequence of
React toggles is not independent when each click changes labels, disabled
states, or the rendered control tree: keep it in one shell tool call, but
re-snapshot between clicks and use the newly returned refs.

For a mini-app, switch into its opaque iframe before taking the interaction
snapshot rather than applying a parent-document selector:

```bash
agent-browser snapshot -i -d 2
# Find: Iframe "<app name>" [ref=eN]
agent-browser frame @eN
agent-browser snapshot -i
# interact and re-snapshot in this frame
agent-browser frame main
```

The shallow parent snapshot creates the documented iframe ref while keeping
shell output small; the explicit frame context then exposes only the app's
interactive descendants. Use the ref rather than a CSS frame selector:
response-sandboxed opaque frames can be absent from the selector resolver even
when their browser frame and accessibility subtree are healthy. Do not weaken
the iframe sandbox. Re-snapshot after state changes and return to `frame main`
before checking shell state.

`agent-browser wait --text` and `wait --fn` can observe the top-level document
rather than the opaque app iframe. For initial load, rely on
`preview_app.sh`'s mounted-frame gate. For an in-app transition, do not invent
a CSS state class for a wait. Use a fresh iframe-scoped snapshot; when the app
has a known bounded animation, one matching bounded wait followed immediately
by that snapshot is preferable to a 25-second timeout.

Two gotchas every session:

- **`@eN` refs are ephemeral** — regenerated on every `snapshot`, invalidated by any DOM change. Re-snapshot before targeting by `@ref` after any mutation. For repeated targets, use a selector only when its matching DOM attribute or structure is verified in the current DOM or source; otherwise re-snapshot and use a fresh ref. A quoted control name in a snapshot is an accessible name, not evidence that a matching DOM attribute exists. `:has-text()` silently no-ops.
- **`✓ Done` only confirms dispatch, not state change** — the CLI returns it the instant the command reaches Chromium, not after the UI changed. Verify with `snapshot` or a screenshot after any click meant to transition UI.
- **Keep screenshots purposeful** — retain the first useful render, a materially changed or error state, and the final evidence. A loader, drawer transition, or near-identical recapture is not a partner-visible milestone.

## Share screenshot evidence with the partner

**This applies to EVERY turn that captures a screenshot** — debugging, audits, app reviews, investigations — not just builds. If you describe what a screenshot shows, the embed must precede the description in the same message.

Loading a PNG into your vision (`Read` on Claude, `view_image` on Codex) lets YOU inspect it. The partner sees ONLY your text plus any `![caption](/api/chats/$CHAT_ID/media/<name>.png)` embeds you explicitly write. The failure mode: you view it, describe it ("the grid rendered beautifully"), but never embed — so the partner trusts an unverified claim. Pattern:

1. `Bash`: capture with `bash "$SCRIPTS_DIR/agent-screenshot.sh" <route>` — with no output path it lands in the chat's served media dir (`/data/chats/$CHAT_ID/media/shot-*.png`) and prints the path **plus a ready-to-paste `![screenshot](/api/chats/…)` embed line** — copy that line into your reply (step 3) so the shot actually shows. For an already-open Möbius state, mint the unique final media path first and use the same helper with `--current-page`; reserve raw `agent-browser screenshot "$OUT"` for non-Möbius pages. Only files under that dir embed; an image created under `/tmp` cannot preview in the chat.
2. `Read` / `view_image`: the path it printed.
3. **Text** (same message, BEFORE interpreting): `![first render](/api/chats/$CHAT_ID/media/<name>.png)` — the embed path must match the file and carry the resolved chat id — a literal `$CHAT_ID` only expands in Bash, never in your markdown. Then a one-line description.
4. Continue.

**If you've seen the app working, the partner should too.** Embed first renders (even broken ones — they let the partner redirect early), major visual changes, working interactions, and especially error/unexpected-state screenshots. Near-identical verification frames can be skipped (judgment call). For structural questions ("does button X exist?"), `snapshot` is enough.

**When the partner reported the bug, reproduce THEIR exact conditions — a proxy that passes is not "fixed."** A headless screenshot settles the DOM but can't exercise a device/PWA-only failure (mobile keyboard, OS gesture bar, scroll-pin, a stale service-worker bundle across a rebuild); `agent-browser` scrolls programmatically, not like a thumb. A happy-path render also doesn't prove a data-driven app is fine — the defect usually lives on the empty/partial/error path (an all-or-nothing fetch that blanks the view). Most *data*-state failures you CAN reproduce headlessly, by seeding that empty/partial/error state first and then screenshotting; only the genuinely device-only classes need their device. When it is one of those, say what you verified and what still needs their device — and don't write "fixed" (a local "tests green" is not "validated").
