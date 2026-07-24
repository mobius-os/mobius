# Visual testing and screenshots

Use this workflow whenever you visually test a Möbius shell or mini-app, drive `agent-browser`, capture a screenshot, inspect a rendered state, or describe screenshot evidence to the partner. The core constitution owns the invariant that visual claims need visible evidence; this skill owns the commands and mechanics.

## Drive the rendered page with agent-browser

`agent-browser` is a CLI wrapping a headless Chromium with a persistent session — your visual testing tool. Seeing the app as it renders beats trusting the code for anything visual.

**To screenshot any Möbius page, use the authenticated helper — never `agent-browser open` it directly.** Your browser starts with an empty `localStorage`, so opening a Möbius URL lands on the login wall and every screenshot is the password form, not the page you meant to capture. The helper writes your scoped token into `localStorage` first, then navigates:

```bash
bash "$SCRIPTS_DIR/agent-screenshot.sh" <route> <out.png>
# /                → the shell      /chat/<id>     → a chat
# /app/<id>        → a mini-app in the shell (numeric id)
# /apps/<slug>/    → a mini-app's standalone PWA page (by slug)
```

`preview_app.sh <id>` and `preview_shell.sh [chat_id]` are thin wrappers over it for those two common cases. `preview_app.sh` is readiness-gated: it waits for the shell's real post-render frame-mounted state, so a successful capture is not merely the loading skeleton. Use the helper, then `Read`/`view_image` the PNG before describing it.

Raw `agent-browser open <url>` is for **non-Möbius pages only** (an external site you're scraping or sanity-checking) — it has no auth dance, so it shows the login wall for any Möbius route.

Core moves once a page is open: `set viewport "$VIEWPORT_WIDTH" "$VIEWPORT_HEIGHT"` (the helper sets this for you; needed when driving raw), `snapshot` (a11y tree with `@eN` refs), `click/fill/type @eN`, `screenshot <path>`, `wait` (on a signal — `wait @eN` / `--text` / `--fn` / `--url` — not a guessed duration), `batch "cmd1" "cmd2"` (ordered, fewer round-trips), `diff snapshot` / `diff screenshot --baseline <before>.png`.

For a mini-app, scope the interaction tree to its iframe rather than dumping the
whole workspace:

```bash
agent-browser snapshot -i -s 'iframe[data-app-id="<app-id>"]'
```

This keeps chats, tabs, and the app drawer out of model context while retaining
the app's interactive descendants. Do **not** use `--compact` with this iframe
selector: current `agent-browser` can collapse it to the iframe node alone.
Use the returned refs for interaction and re-snapshot after state changes.

If the scoped snapshot still returns only the iframe node, do not probe
`contentDocument`, guess coordinates, or repeat equivalent snapshots. Open the
authenticated standalone `/apps/<slug>/` route with
`agent-screenshot.sh`, dismiss any host install prompt from a fresh snapshot,
and use stable app-owned selectors or accessible labels.

`agent-browser wait --text` observes the top-level document and is unreliable
for text inside the opaque app iframe. For initial load, rely on
`preview_app.sh`'s mounted-frame gate. For an in-app transition, use a fresh
iframe-scoped snapshot or wait on a returned element ref rather than spending a
full timeout on top-level text.

Two gotchas every session:

- **`@eN` refs are ephemeral** — regenerated on every `snapshot`, invalidated by any DOM change. Re-snapshot before targeting by `@ref` after any mutation. For repeated targets prefer stable selectors (`button[aria-label="..."]`, `[data-testid="..."]`). `:has-text()` silently no-ops.
- **`✓ Done` only confirms dispatch, not state change** — the CLI returns it the instant the command reaches Chromium, not after the UI changed. Verify with `snapshot` or a screenshot after any click meant to transition UI.
- **Keep screenshots purposeful** — retain the first useful render, a materially changed or error state, and the final evidence. A loader, drawer transition, or near-identical recapture is not a partner-visible milestone.

## Share screenshot evidence with the partner

**This applies to EVERY turn that captures a screenshot** — debugging, audits, app reviews, investigations — not just builds. If you describe what a screenshot shows, the embed must precede the description in the same message.

Loading a PNG into your vision (`Read` on Claude, `view_image` on Codex) lets YOU inspect it. The partner sees ONLY your text plus any `![caption](/api/chats/$CHAT_ID/media/<name>.png)` embeds you explicitly write. The failure mode: you view it, describe it ("the grid rendered beautifully"), but never embed — so the partner trusts an unverified claim. Pattern:

1. `Bash`: capture with `bash "$SCRIPTS_DIR/agent-screenshot.sh" <route>` — with no output path it lands in the chat's served media dir (`/data/chats/$CHAT_ID/media/shot-*.png`) and prints the path **plus a ready-to-paste `![screenshot](/api/chats/…)` embed line** — copy that line into your reply (step 3) so the shot actually shows. (Already-open or non-Möbius page: `agent-browser screenshot /data/chats/$CHAT_ID/media/<name>.png`.) Only files under that dir embed — a bare `agent-browser screenshot /tmp/x.png` is viewable but 404s if embedded.
2. `Read` / `view_image`: the path it printed.
3. **Text** (same message, BEFORE interpreting): `![first render](/api/chats/$CHAT_ID/media/<name>.png)` — the embed path must match the file and carry the resolved chat id — a literal `$CHAT_ID` only expands in Bash, never in your markdown. Then a one-line description.
4. Continue.

**If you've seen the app working, the partner should too.** Embed first renders (even broken ones — they let the partner redirect early), major visual changes, working interactions, and especially error/unexpected-state screenshots. Near-identical verification frames can be skipped (judgment call). For structural questions ("does button X exist?"), `snapshot` is enough.

**When the partner reported the bug, reproduce THEIR exact conditions — a proxy that passes is not "fixed."** A headless screenshot settles the DOM but can't exercise a device/PWA-only failure (mobile keyboard, OS gesture bar, scroll-pin, a stale service-worker bundle across a rebuild); `agent-browser` scrolls programmatically, not like a thumb. A happy-path render also doesn't prove a data-driven app is fine — the defect usually lives on the empty/partial/error path (an all-or-nothing fetch that blanks the view). Most *data*-state failures you CAN reproduce headlessly, by seeding that empty/partial/error state first and then screenshotting; only the genuinely device-only classes need their device. When it is one of those, say what you verified and what still needs their device — and don't write "fixed" (a local "tests green" is not "validated").
