# Image generation

How to generate an image with Codex and get it into the chat. `Read` this before generating an image, and check the `Provider:` line in your injected `<agent_experience>` block first. Möbius does not expose a built-in image-generation path for other providers.

For simple icons or logos, consider an SVG instead — it's crisp, themeable, and reviewable in diffs.

---

## Codex (`$imagegen`)

Codex includes a built-in image generator covered by the plan, with no separate API key needed.

```bash
$imagegen "a serene mountain landscape"
```

The PNG saves under `/data/cli-auth/codex/generated_images/...` and is not
automatically visible in Möbius chat. Publish the exact returned path:

```bash
python "$SCRIPTS_DIR/publish_chat_image.py" "<exact path returned by imagegen>" \
  --alt "short description"
```

Paste the returned `embed` value into the reply before describing the image.
The helper writes to the resolved current chat and deliberately requires the
exact generated path; never rediscover an output by modification time, which
can select another run's image.

---

## Möbius app icons

Möbius app icons are one visual family: a compact sculptural object in
dimensional enamel and polished warm metal, restrained gold or silver
structure, and an occasional small violet jewel. An icon that ignores that
language looks broken beside its neighbours in the launcher.

Use this as the starting prompt whenever the partner asks for an app icon,
unless they want a different art direction. Replace every bracketed field. If
the app already has an icon, attach it as the identity reference and keep
roughly 70% of its defining silhouette, subject, orientation, and palette,
spending the remaining 30% on the shared finish.

```text
Create one isolated app-icon symbol for [APP NAME], an app that [APP PURPOSE].
The central metaphor is [ONE CLEAR, RECOGNIZABLE SYMBOL]. Make the metaphor
immediately legible, distinctive to the app, and readable at 40px.

Match the Möbius icon family: a compact premium sculptural object, dimensional
enamel and polished warm metal, gentle rounded bevels, controlled depth,
restrained highlights, and rich material detail that survives downscaling.
Use [APP-SPECIFIC DOMINANT COLORS] rather than a generic brand palette. A single
small violet jewel may be used as a quiet family accent when it fits the
composition; do not add multiple jewels or decoration without meaning.

Center one bold symbol on a 512x512 canvas with generous transparent padding
and a clean, balanced silhouette. The icon must work on both light and dark
themes. No rounded-square app tile or backplate, no text or letters, no scene,
no floor, no cast/contact shadow, no background glow, no watermark, and no
tiny details that disappear at thumbnail size.

Generate on a perfectly uniform solid #00ff00 chroma-key background for clean
background removal. If the subject itself needs green, use #ff00ff instead.
The key background must contain no gradient, texture, lighting variation,
reflection, or shadow, and its color must not appear in the subject.
```

Two constraints are the easiest to lose and the most expensive to discover
late: **no cast shadow** (a shadow cannot be keyed out and looks wrong in dark
mode) and **the key colour must not appear in the subject** (keying it out
would punch holes in the artwork — that is what the magenta alternative is
for).

### Cutting the key out

```bash
python3 "$SCRIPTS_DIR/remove_chroma_key.py" <generated.png> --out icon.png
```

The key colour is detected from the border, so a flat background needs no
flags. Removal keys on colour distance rather than flooding in from the edges,
so enclosed holes — the gap inside a ring, a loop, a handle — become genuinely
transparent instead of staying filled. Partial-alpha edge pixels have the key's
colour divided back out, which is what prevents the fringe around an otherwise
clean cut-out.

The helper refuses two failures that stay invisible until the icon ships: an
all-transparent result (the key matched the subject — regenerate against the
other key colour) and opaque corners (the background was not actually flat).
Widen `--clear-below` / `--solid-above` only when a soft edge genuinely needs
it.

### Judging the result

Inspect the finished icon at 40px on both a warm light surface and a dark one
before showing it to the partner. Reject key-coloured fringes, clipped
silhouettes, muddy transparency, weak metaphors, and anything that only reads
at full size.

Then keep `icon.png` in the app's source tree and declare it in `mobius.json`.
An icon applied only as a live override leaves the app's own package still
shipping the old artwork, so every other install keeps the icon you replaced.
