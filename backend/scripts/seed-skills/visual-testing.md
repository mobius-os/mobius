# Visual testing and screenshots

Visual-testing extension for Möbius shell and app work. Read it alongside
`building-apps-quickstart.md` for every mini-app build/update, or alongside
`theming.md` for shell UI changes; it owns browser interaction, screenshots,
and visible evidence.

## Drive the rendered page with agent-browser

`agent-browser` is a CLI wrapping a headless Chromium with a persistent session — your visual testing tool. Seeing the app as it renders beats trusting the code for anything visual.

**To screenshot any Möbius page, call the `screenshot` tool — never `agent-browser open` it directly.** Your browser starts with an empty `localStorage`, so opening a Möbius URL lands on the login wall and every screenshot is the password form. The tool runs the authenticated helper at the owner's viewport, saves the image in this chat's served media, and returns the image to you plus the exact embed line for the owner. Routes: `/` the shell, `/shell/?chat=<id>`, `/shell/?app=<id>` (a mini-app in the shell, numeric id), `/apps/<slug>/` (its standalone page); `content_only: true` hides product overlays. It needs write access.

For an already-open page (`--current-page`) or a specific output path, use the
helper itself: `bash "$SCRIPTS_DIR/agent-screenshot.sh" [--current-page] <route> <out.png>`.

`preview_app.sh <id>` and `preview_shell.sh [chat_id]` are thin wrappers over it
for those two common cases. `preview_app.sh` is readiness-gated and uses
ephemeral content-only mode: it waits for the real post-render frame-mounted
state and prevents product-owned walkthrough/install overlays from mounting in
that isolated browser session without writing onboarding or dismissal state.

Raw `agent-browser open <url>` is for **non-Möbius pages only** (an external site you're scraping or sanity-checking) — it has no auth dance, so it shows the login wall for any Möbius route.

After changing state or temporarily injecting CSS into an already-open Möbius
page, capture the result through the same verified boundary without navigating:

```bash
bash "$SCRIPTS_DIR/agent-screenshot.sh" --current-page <route> <out.png>
```

Do **not** fall back to raw `agent-browser screenshot` for a Möbius comparison;
that bypasses the real display density, exact-font readiness, freshness, and
atomic-output checks. Raw capture remains appropriate for non-Möbius pages.

The helper always applies `$VIEWPORT_WIDTH`/`$VIEWPORT_HEIGHT`, including on
`--current-page`, so a manual `agent-browser set viewport` is overwritten. To
capture a different width, override the environment variables for that one
invocation (`VIEWPORT_WIDTH=390 VIEWPORT_HEIGHT=844 bash
"$SCRIPTS_DIR/agent-screenshot.sh" …`). The helper's auth probe requires the
server to be reachable: it cannot capture while the backend is down, by design.

Font fidelity: if the container's fontconfig lacks a sans-serif alias (a fresh
container before setup restores it), generic `sans-serif`/`system-ui` stacks
resolve to a monospace face in captures even though the owner's devices render
them correctly. When verifying typography, put a concrete proportional family
(for example `'Liberation Sans', Arial`) ahead of the generics, or check
`fc-match sans-serif` before trusting the rendered face.

Core moves once a page is open: `set viewport "$VIEWPORT_WIDTH" "$VIEWPORT_HEIGHT" "$VIEWPORT_PIXEL_RATIO"` (the helper sets the complete geometry for you; needed when driving raw non-Möbius pages), `snapshot` (a11y tree with `@eN` refs), `click/fill/type @eN`, `wait` (on a signal — `wait @eN` / `--text` / `--fn` / `--url` — not a guessed duration), `batch "cmd1" "cmd2"` (ordered, fewer round-trips), `diff snapshot` / `diff screenshot --baseline <before>.png`.

**Write a shareable image once, at its final home.** Any screenshot, render,
crop, montage, or other raster image you intend to embed in your reply must be
created directly under `/data/chats/$CHAT_ID/media/`, not created in `/tmp` and
copied afterward. The chat can preview raster files you inspect under `/tmp`
inside the protected tool activity, but temporary files still cannot be used as
durable message embeds. Mint a unique final path before the producing command:

```bash
MEDIA_DIR="/data/chats/$CHAT_ID/media"
mkdir -p "$MEDIA_DIR"
OUT="$MEDIA_DIR/inspect-$(date +%s%N).png"
bash "$SCRIPTS_DIR/agent-screenshot.sh" --current-page <route> "$OUT"
```

`/tmp` remains correct for logs, diffs, test workspaces, disposable browser
warm-ups, and images you only need to inspect through `Read`/`view_image`. When
an external tool controls its own output location and you need to show that
image in your reply, publish the unavoidable pre-existing file once with
`publish_chat_image.py`; do not make `/tmp` the default for shareable images
you create yourself.

For textboxes, use `fill @eN "value"` directly. Do not split that into
`click`, `Control+A`, and a selector-less `type`; the extra commands add
round-trips and the final `type` has no target. **The Möbius chat composer is
the exception:** it places the caret at the end on focus and keeps a persisted
draft, so `fill` appends to the existing text and an empty `fill` does not
clear it. Drive the composer with `click @eN`, then `press Control+a`, then
`type` the new text (or `press Backspace` to clear). When a test needs a pause,
a bare leading `sleep N` is refused in this environment — use
`python3 -c "import time; time.sleep(N)"`. Batch independent operations
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

The frame context scopes refs and snapshots, not everything:

- `eval` always runs in the shell document, even after `frame @eN`. Read app
  state from a frame-scoped `snapshot` instead.
- Raw `mouse` commands use page coordinates and do reach the app, but
  `get box` inside the frame reports frame-relative coordinates. Passing those
  to `mouse move` misses the target.
- Ref-based `click`/`hover @eN` inside an app iframe can also miss: the
  computed point is shifted by the iframe's own page offset at wide viewports,
  so the click is refused as "covered by" another element (including a
  frame-internal overlay such as a modal sheet's scrim) or lands on the wrong
  element — even after a `✓ Done`. Prefer `focus @eN` + `press Enter`/`Space`
  for buttons and `fill`/`select` for inputs when interacting inside an app
  iframe; use pointer clicks there only when a keyboard path genuinely cannot
  trigger the behavior.
- To scroll, use `scroll <dir>` or `scrollintoview <sel>`; `mouse wheel`
  dispatches at (0,0) regardless of any prior `mouse move`, so it scrolls the
  page, not the element under the pointer. For an inner scroller that neither
  command reaches, dispatch a CDP `Input.dispatchMouseEvent` wheel at the
  element's coordinates via `get cdp-url`.

For press-and-hold or drag, let a ref place the pointer, then press:

```bash
agent-browser hover @eN    # an app ref from the frame snapshot
agent-browser mouse down
agent-browser wait 800
agent-browser mouse up
```

For a canvas point with no ref, add the in-frame offset to the iframe's own
page box, read with `get box` on the iframe ref from `frame main`.

`agent-browser wait --text` and `wait --fn` can observe the top-level document
rather than the opaque app iframe. For initial load, rely on
`preview_app.sh`'s mounted-frame gate. For an in-app transition, do not invent
a CSS state class for a wait. Use a fresh iframe-scoped snapshot; when the app
has a known bounded animation, one matching bounded wait followed immediately
by that snapshot is preferable to a 25-second timeout.

Session gotchas:

- **The default session uses a persistent per-chat browser profile**, so
  in-page UI state — open disclosures, scroll positions, entered drafts —
  survives from earlier turns and sessions. A control may already be in the
  state a fresh page would not have, and a blind first click toggles it the
  wrong way. Read the control's current state from a snapshot (for example
  `aria-expanded`) before toggling or measuring, rather than assuming the
  pristine default.
- **With agent-browser 0.38+, surviving elements keep their `@eN` refs across same-document changes.** Replaced elements and navigated documents/frames invalidate refs. Take a fresh snapshot after a transition rather than assuming the old target survived. For repeated targets, use a selector only when its matching DOM attribute or structure is verified in the current DOM or source; otherwise re-snapshot and use a fresh ref. A quoted control name in a snapshot is an accessible name, not evidence that a matching DOM attribute exists. `:has-text()` silently no-ops.
- **`✓ Done` only confirms dispatch, not state change** — the CLI returns it the instant the command reaches Chromium, not after the UI changed. Verify with `snapshot` or a screenshot after any click meant to transition UI.
- **Keep screenshots purposeful** — retain the first useful render, a materially changed or error state, and the final evidence. A loader, drawer transition, or near-identical recapture is not a partner-visible milestone.

## Share screenshot evidence with the partner

**This applies to EVERY turn that captures a screenshot** — debugging, audits, app reviews, investigations — not just builds. If you describe what a screenshot shows, the embed must precede the description in the same message.

**Served before spoken.** Before emitting an image embed, the exact file must
already exist under the chat's served `media/` directory. Confirm that it is
non-empty and that its authenticated `/api/chats/<chat-id>/media/<name>` URL
returns `200` with an image content type. Never post an embed first and copy or
verify the file in a later tool call: the client fetches immediately and may
retain the broken result.

Loading a PNG into your vision (`Read` on Claude, `view_image` on Codex) lets YOU inspect it. The partner sees ONLY your text plus any `![caption](/api/chats/$CHAT_ID/media/<name>.png)` embeds you explicitly write. The failure mode: you view it, describe it ("the grid rendered beautifully"), but never embed — so the partner trusts an unverified claim. Pattern:

1. Capture with the `screenshot` tool: the file lands in the chat's served media dir and the result carries the image itself **plus a ready-to-paste `![screenshot](/api/chats/…)` embed line**, so steps 2–3 are already done for it. For an already-open Möbius state, mint the unique final media path first and run `agent-screenshot.sh --current-page` with it; reserve raw `agent-browser screenshot "$OUT"` for non-Möbius pages. For an upload or unavoidable pre-existing tool output, publish the exact file with `publish_chat_image.py`. Only files under `media/` embed in replies; `/tmp` images preview only inside their protected tool activity.
2. `Bash`: verify the final media file and authenticated response as above.
3. `Read` / `view_image`: inspect that final media file.
4. **Text** (same message, BEFORE interpreting): paste the verified embed. The path must carry the resolved chat id; a literal `$CHAT_ID` only expands in Bash, never in markdown. Then add the one-line description.
5. Continue.

**If you've seen the app working, the partner should too.** Embed first renders (even broken ones — they let the partner redirect early), major visual changes, working interactions, and especially error/unexpected-state screenshots. Near-identical verification frames can be skipped (judgment call). For structural questions ("does button X exist?"), `snapshot` is enough.

**When the partner reported the bug, reproduce THEIR exact conditions — a proxy that passes is not "fixed."** A headless screenshot settles the DOM but can't exercise a device/PWA-only failure (mobile keyboard, OS gesture bar, scroll-pin, a stale service-worker bundle across a rebuild); `agent-browser` scrolls programmatically, not like a thumb. A happy-path render also doesn't prove a data-driven app is fine — the defect usually lives on the empty/partial/error path (an all-or-nothing fetch that blanks the view). Most *data*-state failures you CAN reproduce headlessly, by seeding that empty/partial/error state first and then screenshotting; only the genuinely device-only classes need their device. When it is one of those, say what you verified and what still needs their device — and don't write "fixed" (a local "tests green" is not "validated").

## Efficient inspection with agent-browser 0.38+

Use `snapshot --delta` for repeated structural inspection: the first response
is a baseline and later responses contain changes. Use `snapshot --delta --full` when a fresh
baseline is needed. This is not a replacement for rendered verification.
Take fewer, purposeful captures rather than many near-identical ones.
A timed-out `--current-page` capture cleans up its poisoned browser but cannot
restore injected CSS or unsaved page state; prepare that state again explicitly.

## Which browser runs

On amd64 images agent-browser uses Chrome for Testing's **headless shell** by
default: Chrome's rendering engine without the full browser, light enough for
a 1 GB server. It covers the screenshot tool, app frames, snapshots, clicks,
scripts, PDF output, offline mode, and tabs. arm64 images use Debian's
Chromium instead.

Full Chrome stays installed for what only it does: browser extensions and
full-browser features such as its built-in PDF viewer. It needs roughly
500 MB on top of the server and agents, so check the container limit first
with `cat /sys/fs/cgroup/memory.max` (`max` means unlimited). **Below 2 GiB
(2147483648), do not start it:** it thrashes memory, times out, and slows
every other chat. Tell the owner plainly that the task needs full Chrome,
how much memory their server has, that full Chrome needs about 500 MB of it,
and that raising the server's memory to 2 GB would make it possible. Keep
using the default browser for everything else. If the owner still asks you
to try, warn once, then proceed.

This recipe is for amd64 only: arm64 images have no separate full Chrome,
and their default Chromium already is the full browser. Use full Chrome
in its own session with a throwaway profile named after that session, so the
default session keeps its identity and no other session shares the profile.
Shell state does not carry between commands, so start every command that uses
full Chrome, including the cleanup, with these definitions. They always
resolve to the same session and profile, and stop with an error if a value is
missing:

```bash
FULL_CHROME="$(ls -d /opt/agent-browser/browsers/chrome-*/chrome | head -n 1)"
FULL_SESSION="${AGENT_BROWSER_SESSION:?}-full"
FULL_PROFILE="${TMPDIR:?}/agent-browser-$FULL_SESSION"
full() {
  AGENT_BROWSER_SESSION="$FULL_SESSION" \
  AGENT_BROWSER_PROFILE="$FULL_PROFILE" \
  AGENT_BROWSER_EXECUTABLE_PATH="${FULL_CHROME:?}" agent-browser "$@"
}
```

Then use it, for example `full open <url>`. When done, close it, confirm
nothing still uses the profile, then delete the profile (after the same
definitions):

```bash
full close
python3 "$SCRIPTS_DIR/agent_browser_session_reset.py" "${FULL_PROFILE:?}" && rm -rf "$FULL_PROFILE"
```

If `full close` hangs, stop it and run only the reset line. If the reset
fails, leave the profile in place and name its path for the next agent.

## Close the browser session when you are done

`agent-browser` leaves its browser process tree alive after the turn. Close the
session in the same turn you finish visual work; do not retain it in case of a
follow-up. If it cannot close cleanly, name the profile
(`/data/agent-browser-profiles/chat-<chat-id>`) for the next agent. When graceful close fails, use
`python3 "$SCRIPTS_DIR/agent_browser_session_reset.py" "$AGENT_BROWSER_PROFILE"`.
The shared lifecycle owner verifies exact process identities, includes orphan
Chrome/helpers, and refuses uncertain ownership. Never use `pkill -f`.

Never run `agent-browser close --all`: every chat on this server shares one
session directory, so it closes other chats' browsers too. A second browser
doubles memory, so work in the chat's own session. If a task truly needs
another session, put its profile under `$TMPDIR`, never in
`/data/agent-browser-profiles/`, and close it in the same turn.
